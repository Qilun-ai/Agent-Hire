import { createServer, request as httpRequest, type IncomingMessage, type ServerResponse } from "node:http";
import { connect } from "node:net";
import { createReadStream, existsSync, statSync } from "node:fs";
import { extname, join, normalize, resolve, sep } from "node:path";
import { Readable } from "node:stream";
import { timingSafeEqual } from "node:crypto";
import type { AgentConfig } from "./config.js";
import type { ApiClient } from "./api-client.js";
import type { ChatHost } from "./sessions.js";
import type { Scheduler } from "./scheduler.js";
import type { Uploads } from "./uploads.js";
import type { Preferences } from "./preferences.js";
import { LlmNotConfigured, type LlmManager } from "./llm.js";

export interface ServerDeps {
	config: AgentConfig;
	api: ApiClient;
	host: ChatHost;
	scheduler: Scheduler;
	uploads: Uploads;
	preferences: Preferences;
	llm: LlmManager;
}

const MIME: Record<string, string> = {
	".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
	".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
	".woff2": "font/woff2", ".woff": "font/woff", ".map": "application/json", ".webp": "image/webp", ".txt": "text/plain",
};

const LOOPBACK_HOST = /^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$/i;

function json(res: ServerResponse, status: number, body: unknown): void {
	const data = JSON.stringify(body);
	res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
	res.end(data);
}

async function readJson(req: IncomingMessage, limit = 1_000_000): Promise<any> {
	const chunks: Buffer[] = [];
	let size = 0;
	for await (const chunk of req) {
		size += chunk.length;
		if (size > limit) throw Object.assign(new Error("Body too large"), { status: 413 });
		chunks.push(chunk as Buffer);
	}
	if (!chunks.length) return {};
	try {
		return JSON.parse(Buffer.concat(chunks).toString("utf8"));
	} catch {
		throw Object.assign(new Error("Invalid JSON"), { status: 400 });
	}
}

function safeEqual(a: string, b: string): boolean {
	const x = Buffer.from(a);
	const y = Buffer.from(b);
	return x.length === y.length && timingSafeEqual(x, y);
}

export function createAppServer(deps: ServerDeps) {
	const { config, api, host, scheduler, uploads, preferences, llm } = deps;
	const apiUrl = new URL(config.apiUrl);
	const webRoot = config.webDist && existsSync(join(config.webDist, "index.html")) ? resolve(config.webDist) : "";

	/** /agent/* uses the same bearer token as the API (EventSource cannot send headers, so ?token= too). */
	async function authorized(req: IncomingMessage, url: URL): Promise<boolean> {
		const header = req.headers.authorization ?? "";
		const presented = header.startsWith("Bearer ") ? header.slice(7) : url.searchParams.get("token") ?? "";
		if (!presented) return false;
		for (let attempt = 0; attempt < 2; attempt++) {
			const expected = (await api.authHeaders()).Authorization.slice(7);
			if (safeEqual(presented, expected)) return true;
			// The API restarted and rotated its token: refresh ours once and compare again.
			(api as unknown as { token: string }).token = "";
		}
		return false;
	}

	function proxy(req: IncomingMessage, res: ServerResponse): void {
		const upstream = httpRequest(
			{
				hostname: apiUrl.hostname,
				port: apiUrl.port,
				method: req.method,
				path: req.url,
				headers: { ...req.headers, host: `127.0.0.1:${apiUrl.port}` },
			},
			(up) => {
				res.writeHead(up.statusCode ?? 502, up.headers);
				up.pipe(res);
			},
		);
		upstream.on("error", () => {
			if (!res.headersSent) json(res, 502, { detail: "Agent-Hire API is not reachable" });
			else res.end();
		});
		req.pipe(upstream);
	}

	function serveStatic(req: IncomingMessage, res: ServerResponse, pathname: string): boolean {
		if (!webRoot) return false;
		const target = normalize(join(webRoot, decodeURIComponent(pathname)));
		if (target !== webRoot && !target.startsWith(webRoot + sep)) return false;
		let file = target;
		if (!existsSync(file) || statSync(file).isDirectory()) {
			// SPA fallback: unknown paths without an extension render the app shell.
			if (extname(pathname)) return false;
			file = join(webRoot, "index.html");
		}
		const type = MIME[extname(file)] ?? "application/octet-stream";
		const immutable = file.includes(`${sep}assets${sep}`);
		res.writeHead(200, { "Content-Type": type, "Cache-Control": immutable ? "public, max-age=31536000, immutable" : "no-cache" });
		if (req.method === "HEAD") return void res.end(), true;
		createReadStream(file).pipe(res);
		return true;
	}

	function sse(res: ServerResponse): (event: unknown) => void {
		res.writeHead(200, {
			"Content-Type": "text/event-stream",
			"Cache-Control": "no-store",
			Connection: "keep-alive",
			"X-Accel-Buffering": "no",
		});
		res.write(": connected\n\n");
		const ping = setInterval(() => res.write(": ping\n\n"), 20_000);
		res.on("close", () => clearInterval(ping));
		return (event) => res.write(`data: ${JSON.stringify(event)}\n\n`);
	}

	async function handleAgent(req: IncomingMessage, res: ServerResponse, url: URL): Promise<void> {
		const path = url.pathname.replace(/\/+$/, "") || "/";
		const method = req.method ?? "GET";
		if (path === "/agent/health") return json(res, 200, { status: "ok" });
		if (!(await authorized(req, url))) return json(res, 401, { detail: "Missing or invalid token" });

		if (path === "/agent/status" && method === "GET") {
			let llmStatus: Record<string, unknown>;
			try {
				const model = await llm.currentModel(true);
				llmStatus = { configured: true, ...llm.describe(), model: model.id };
			} catch (error) {
				llmStatus = { configured: false, reason: (error as Error).message };
			}
			return json(res, 200, { llm: llmStatus, schedules: scheduler.list().length });
		}
		if (path === "/agent/events" && method === "GET") {
			const send = sse(res);
			const off = host.onHostEvent(send);
			res.on("close", off);
			return;
		}
		if (path === "/agent/sessions" && method === "GET") return json(res, 200, { sessions: await host.listSessions() });
		if (path === "/agent/sessions" && method === "POST") return json(res, 201, { id: await host.createSession() });
		if (path === "/agent/preferences" && method === "GET") return json(res, 200, await preferences.get());
		if (path === "/agent/preferences" && method === "PUT") return json(res, 200, await preferences.update(await readJson(req)));
		if (path === "/agent/schedules" && method === "GET") {
			return json(res, 200, { schedules: scheduler.list(url.searchParams.get("session") ?? undefined) });
		}
		const scheduleMatch = path.match(/^\/agent\/schedules\/([\w-]+)$/);
		if (scheduleMatch && method === "DELETE") return json(res, 200, { removed: await scheduler.remove(scheduleMatch[1]) });

		if (path === "/agent/uploads" && method === "POST") {
			const webReq = new Request(`http://local${req.url}`, {
				method: "POST",
				headers: req.headers as Record<string, string>,
				body: Readable.toWeb(req) as ReadableStream,
				duplex: "half",
			} as RequestInit);
			const form = await webReq.formData();
			const file = form.get("file");
			if (!(file instanceof File)) return json(res, 400, { detail: "Send the file in a 'file' field" });
			try {
				const record = await uploads.save(file.name, file.type, new Uint8Array(await file.arrayBuffer()));
				return json(res, 201, { upload_id: record.id, name: record.name, size: record.size, mime: record.mime });
			} catch (error) {
				return json(res, 400, { detail: (error as Error).message });
			}
		}

		const m = path.match(/^\/agent\/sessions\/([\w-]+)(?:\/(messages|events|abort))?$/);
		if (m) {
			const [, id, action] = m;
			if (!(await host.exists(id))) return json(res, 404, { detail: "No such chat" });
			if (!action && method === "GET") return json(res, 200, await host.transcript(id));
			if (!action && method === "DELETE") return json(res, 200, { deleted: await host.deleteSession(id) });
			if (action === "abort" && method === "POST") {
				await host.abort(id);
				return json(res, 200, { aborted: true });
			}
			if (action === "messages" && method === "POST") {
				const body = await readJson(req);
				const text = typeof body.text === "string" ? body.text.trim() : "";
				const attachments: string[] = Array.isArray(body.attachments) ? body.attachments.filter((a: unknown) => typeof a === "string") : [];
				if (!text && !attachments.length) return json(res, 400, { detail: "Empty message" });
				const notes: string[] = [];
				for (const uploadId of attachments.slice(0, 10)) {
					const record = await uploads.get(uploadId);
					if (record) notes.push(`[Attached file: ${record.name} (${Math.round(record.size / 1024)} KB, upload_id=${record.id})]`);
				}
				const message = [text, ...notes].filter(Boolean).join("\n");
				try {
					await host.send(id, message.slice(0, 20_000));
					return json(res, 202, { accepted: true });
				} catch (error) {
					const status = error instanceof LlmNotConfigured ? 409 : 500;
					return json(res, status, { detail: (error as Error).message });
				}
			}
			if (action === "events" && method === "GET") {
				await host.transcript(id); // ensures the session is live so events can flow
				const send = sse(res);
				const after = Number(url.searchParams.get("after") ?? 0) || 0;
				const { unsubscribe, resync } = host.subscribe(id, after, send);
				if (resync) send({ type: "resync" });
				res.on("close", unsubscribe);
				return;
			}
		}
		return json(res, 404, { detail: "Unknown agent route" });
	}

	const server = createServer(async (req, res) => {
		try {
			if (!LOOPBACK_HOST.test(req.headers.host ?? "")) return json(res, 400, { detail: "Invalid host" });
			const url = new URL(req.url ?? "/", "http://localhost");
			if (url.pathname.startsWith("/agent/") || url.pathname === "/agent") return await handleAgent(req, res, url);
			const toApi = /^\/(api|bootstrap|health|docs|openapi\.json|ws)(\/|$)/.test(url.pathname);
			if (!toApi && (req.method === "GET" || req.method === "HEAD") && serveStatic(req, res, url.pathname)) return;
			return proxy(req, res);
		} catch (error) {
			const status = (error as { status?: number }).status ?? 500;
			if (!res.headersSent) json(res, status, { detail: (error as Error).message });
			else res.end();
		}
	});

	// WebSocket (/ws) passthrough to the API's live-update socket.
	server.on("upgrade", (req, socket, head) => {
		if (!LOOPBACK_HOST.test(req.headers.host ?? "")) return socket.destroy();
		const upstream = connect(Number(apiUrl.port), apiUrl.hostname, () => {
			const lines = [`${req.method} ${req.url} HTTP/1.1`];
			for (let i = 0; i < req.rawHeaders.length; i += 2) {
				const name = req.rawHeaders[i];
				const value = name.toLowerCase() === "host" ? `127.0.0.1:${apiUrl.port}` : req.rawHeaders[i + 1];
				lines.push(`${name}: ${value}`);
			}
			upstream.write(lines.join("\r\n") + "\r\n\r\n");
			if (head.length) upstream.write(head);
			upstream.pipe(socket);
			socket.pipe(upstream);
		});
		upstream.on("error", () => socket.destroy());
		socket.on("error", () => upstream.destroy());
	});

	return server;
}
