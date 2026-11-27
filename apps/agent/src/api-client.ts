import type { AgentConfig } from "./config.js";

/** Error from the Agent-Hire API, with the status and the server's detail message. */
export class ApiError extends Error {
	constructor(
		readonly status: number,
		readonly detail: string,
		readonly path: string,
	) {
		super(`API ${status} on ${path}: ${detail}`);
	}
}

/**
 * Client for the Python API. It authenticates exactly like the browser UI: the bearer token comes from
 * /bootstrap (loopback only). The token rotates when the API restarts, so a 401 refreshes it once.
 */
export class ApiClient {
	private token = "";

	constructor(private readonly config: AgentConfig) {}

	get baseUrl(): string {
		return this.config.apiUrl;
	}

	private async refreshToken(): Promise<void> {
		const res = await fetch(`${this.config.apiUrl}/bootstrap`);
		if (!res.ok) throw new ApiError(res.status, "bootstrap failed", "/bootstrap");
		const body = (await res.json()) as { token?: string };
		this.token = body.token ?? "";
	}

	async authHeaders(): Promise<Record<string, string>> {
		if (!this.token) await this.refreshToken();
		return { Authorization: `Bearer ${this.token}` };
	}

	async request<T = unknown>(
		method: string,
		path: string,
		body?: unknown,
		options: { headers?: Record<string, string>; timeoutMs?: number; form?: FormData } = {},
	): Promise<T> {
		for (let attempt = 0; attempt < 2; attempt++) {
			const headers: Record<string, string> = { ...(await this.authHeaders()), ...(options.headers ?? {}) };
			let payload: BodyInit | undefined;
			if (options.form) payload = options.form;
			else if (body !== undefined) {
				headers["Content-Type"] = "application/json";
				payload = JSON.stringify(body);
			}
			const res = await fetch(`${this.config.apiUrl}${path}`, {
				method,
				headers,
				body: payload,
				signal: AbortSignal.timeout(options.timeoutMs ?? 120_000),
			});
			if (res.status === 401 && attempt === 0) {
				this.token = "";
				continue;
			}
			const text = await res.text();
			let data: unknown = undefined;
			try {
				data = text ? JSON.parse(text) : undefined;
			} catch {
				data = text;
			}
			if (!res.ok) {
				const detail =
					data && typeof data === "object" && "detail" in data
						? typeof (data as { detail: unknown }).detail === "string"
							? (data as { detail: string }).detail
							: JSON.stringify((data as { detail: unknown }).detail)
						: String(text).slice(0, 500);
				throw new ApiError(res.status, detail, path);
			}
			return data as T;
		}
		throw new ApiError(401, "unauthorized", path);
	}

	get<T = unknown>(path: string, timeoutMs?: number): Promise<T> {
		return this.request<T>("GET", path, undefined, { timeoutMs });
	}

	post<T = unknown>(path: string, body?: unknown, timeoutMs?: number): Promise<T> {
		return this.request<T>("POST", path, body ?? {}, { timeoutMs });
	}

	put<T = unknown>(path: string, body?: unknown): Promise<T> {
		return this.request<T>("PUT", path, body ?? {});
	}

	/** Resolved LLM endpoint for the agent; needs the launcher-provided secret. */
	llmConfig(): Promise<LlmConfig> {
		return this.request<LlmConfig>("GET", "/api/v1/agent-internal/llm-config", undefined, {
			headers: { "X-Agent-Secret": this.config.agentSecret },
			timeoutMs: 15_000,
		});
	}
}

export interface LlmConfig {
	provider?: string;
	api?: "openai-completions" | "anthropic-messages";
	base_url?: string;
	api_key?: string;
	model?: string;
	reasoning?: string;
	error?: string;
	message?: string;
}
