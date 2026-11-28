/**
 * The agent reads a résumé itself (no backend parser) and saves the profile it extracted.
 * Runs against a RUNNING API with a scripted (faux) model:  AGENT_HIRE_API_URL=http://127.0.0.1:8310
 * Use an isolated API instance — it writes a profile.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { ApiClient } from "../src/api-client.js";
import { Scheduler } from "../src/scheduler.js";
import { Uploads } from "../src/uploads.js";
import { Preferences } from "../src/preferences.js";
import { ChatHost, type ChatEvent } from "../src/sessions.js";
import type { LlmManager } from "../src/llm.js";

const API = process.env.AGENT_HIRE_API_URL ?? "http://127.0.0.1:8310";

test("agent imports a résumé, reads it, and saves the profile itself", { timeout: 180_000 }, async (t) => {
	try {
		await fetch(`${API}/health`, { signal: AbortSignal.timeout(2000) });
	} catch {
		return t.skip(`no API at ${API}`);
	}
	const dir = mkdtempSync(join(tmpdir(), "gh-doc-test-"));
	const api = new ApiClient({ port: 0, host: "127.0.0.1", apiUrl: API, agentSecret: "", dataDir: dir, webDist: "" });
	const runtime = await ModelRuntime.create({ authPath: join(dir, "auth.json"), modelsPath: null, refreshOnCreate: false });
	const faux = fauxProvider({ provider: "faux-doc" });
	runtime.registerNativeProvider(faux.provider);
	const model = faux.getModel();
	const llm = { modelRuntime: runtime, currentModel: async () => model, describe: () => ({}) } as unknown as LlmManager;
	const uploads = new Uploads(join(dir, "uploads"));
	let host!: ChatHost;
	const scheduler = new Scheduler(dir, (s) => host.wake(s));
	host = new ChatHost(dir, llm, { api, scheduler, uploads, preferences: new Preferences(dir) });

	const pdf = readFileSync(new URL("./fixtures/sample-resume.pdf", import.meta.url));
	const upload = await uploads.save("asha-resume.pdf", "application/pdf", new Uint8Array(pdf));

	faux.setResponses([
		fauxAssistantMessage([fauxToolCall("import_document", { upload_id: upload.id, kind: "resume" })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxToolCall("save_profile", {
			candidate: { name: "Asha Verma", summary: "Final-year computer science student who builds backend services and distributed systems." },
			identity: { email: "asha.verma@example.com", github_url: "https://github.com/ashaverma", city: "Bengaluru, India" },
			skills: [{ name: "Python", category: "language" }, { name: "Go", category: "language" }, { name: "Kafka", category: "data" }, { name: "Kubernetes", category: "devops" }],
			experience: [{ role: "Backend Engineering Intern", company: "Razorpay", period: "May 2025 – Aug 2025",
				description: "- Built a Kafka consumer in Go that reconciles 2 million payment events per day.\n- Cut p99 latency of the settlements API by 35% by adding PostgreSQL indexes and caching." }],
			projects: [{ title: "ShardKV", stack: "Go, Raft, Docker", impact: "A replicated key-value store with leader election and log compaction." }],
			education: [{ title: "B.Tech Computer Science, IIIT Hyderabad (2022–2026)" }],
		})], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxText("Saved your profile: Asha Verma, 4 skills, 1 role, 1 project.")]),
	]);

	const id = await host.createSession();
	const events: ChatEvent[] = [];
	host.subscribe(id, 0, (e) => events.push(e));
	await host.send(id, `Here's my résumé\n[Attached file: asha-resume.pdf (65 KB, upload_id=${upload.id})]`);
	const started = Date.now();
	while (!events.some((e) => e.type === "run_end") && Date.now() - started < 120_000) await new Promise((r) => setTimeout(r, 50));

	const tools = events.filter((e) => e.type === "tool_end");
	assert.deepEqual(tools.map((e) => e.name), ["import_document", "save_profile"]);
	for (const e of tools) assert.equal(e.isError, false, `${e.name}: ${e.result}`);
	const read = String(tools[0].result);
	assert.match(read, /Asha Verma/);
	assert.match(read, /Razorpay/);
	assert.match(read, /reconciles 2 million payment events/, "words keep their spacing");

	const profile = await api.get<any>("/api/v1/profile");
	assert.equal(profile.n, "Asha Verma");
	const skills = (profile.skills ?? []).map((s: any) => String(s.n ?? s.name).toLowerCase());
	for (const s of ["python", "go", "kafka", "kubernetes"]) assert.ok(skills.includes(s), `skill ${s}`);
	assert.ok((profile.exp ?? []).some((e: any) => /Razorpay/.test(e.co ?? e.company ?? "")), "experience saved");
	assert.ok((profile.projects ?? []).some((p: any) => /ShardKV/.test(p.title ?? "")), "project saved");
	const docs = await api.get<any>("/api/v1/documents");
	const list = Array.isArray(docs) ? docs : docs.documents ?? docs.items ?? [];
	assert.ok(list.some((d: any) => /asha-resume/.test(JSON.stringify(d))), "résumé stored in documents");

	await host.deleteSession(id);
	host.dispose();
	scheduler.stop();
});
