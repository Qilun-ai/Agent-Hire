/**
 * The agent manages the user's data on request: profile edits, custom jobs, pipeline changes, resets.
 * Runs against a RUNNING, ISOLATED API (own app data AND own job index) — it resets the index.
 *   AGENT_HIRE_API_URL=http://127.0.0.1:8310 AGENT_HIRE_ISOLATED=1 npm test
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
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

test("agent edits the profile and manages jobs on request", { timeout: 240_000 }, async (t) => {
	if (process.env.AGENT_HIRE_ISOLATED !== "1") return t.skip("needs an isolated API (set AGENT_HIRE_ISOLATED=1)");
	const dir = mkdtempSync(join(tmpdir(), "gh-data-test-"));
	const api = new ApiClient({ port: 0, host: "127.0.0.1", apiUrl: API, agentSecret: "", dataDir: dir, webDist: "" });
	const runtime = await ModelRuntime.create({ authPath: join(dir, "auth.json"), modelsPath: null, refreshOnCreate: false });
	const faux = fauxProvider({ provider: "faux-data" });
	runtime.registerNativeProvider(faux.provider);
	const model = faux.getModel();
	const llm = { modelRuntime: runtime, currentModel: async () => model, describe: () => ({}) } as unknown as LlmManager;
	let host!: ChatHost;
	const scheduler = new Scheduler(dir, (s) => host.wake(s));
	host = new ChatHost(dir, llm, { api, scheduler, uploads: new Uploads(join(dir, "uploads")), preferences: new Preferences(dir) });
	const id = await host.createSession();
	const events: ChatEvent[] = [];
	host.subscribe(id, 0, (e) => events.push(e));

	const run = async (text: string) => {
		const before = events.length;
		await host.send(id, text);
		const started = Date.now();
		while (!events.slice(before).some((e) => e.type === "run_end") && Date.now() - started < 120_000) await new Promise((r) => setTimeout(r, 50));
		const tools = events.slice(before).filter((e) => e.type === "tool_end");
		for (const e of tools) assert.equal(e.isError, false, `${e.name}: ${e.result}`);
		return tools;
	};

	// Seed a profile, then edit it.
	await api.post("/api/v1/ingest/profile", {
		candidate: { name: "Test User", summary: "Backend developer" },
		skills: [{ name: "Java", category: "language" }, { name: "Python", category: "language" }],
		experience: [{ role: "Intern", company: "Acme", period: "2025", description: "- Built APIs" }],
	});
	const profile = await api.get<any>("/api/v1/profile");
	const java = profile.skills.find((s: any) => (s.n ?? s.name) === "Java");
	const role = profile.exp[0];

	faux.setResponses([
		fauxAssistantMessage([fauxToolCall("edit_profile", { action: "delete", section: "skill", ids: [java.id] })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxToolCall("edit_profile", { action: "update", section: "experience", ids: [role.id], fields: { role: "Backend Intern", description: "- Built REST APIs in Go\n- Added tracing" } })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxToolCall("edit_profile", { action: "add", section: "certification", fields: { title: "AWS Cloud Practitioner (2025)" } })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxToolCall("edit_profile", { action: "update", section: "basics", fields: { name: "Test User Two", github_url: "https://github.com/test" } })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxText("Done.")]),
	]);
	await run("Remove Java, rename my intern role, add my AWS cert and my GitHub.");
	const edited = await api.get<any>("/api/v1/profile");
	const names = edited.skills.map((s: any) => s.n ?? s.name);
	assert.ok(!names.includes("Java") && names.includes("Python"), `skills now ${names}`);
	assert.equal(edited.exp[0].role, "Backend Intern");
	assert.match(edited.exp[0].d, /REST APIs in Go/);
	assert.equal(edited.exp[0].co, "Acme", "untouched fields are kept");
	assert.ok(JSON.stringify(edited.certifications).includes("AWS Cloud Practitioner"));
	assert.equal(edited.n, "Test User Two");
	assert.equal(edited.identity.github_url, "https://github.com/test");

	// Custom job → pipeline → stage change → delete → reset.
	faux.setResponses([
		fauxAssistantMessage([fauxToolCall("add_custom_job", { title: "Platform Engineer", company: "Example Corp", description: "Go, Kubernetes, 1+ years of experience.", url: "https://example.com/jobs/42", location: "Remote" })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxText("Added.")]),
	]);
	const [added] = await run("Add this job: Platform Engineer at Example Corp …");
	const ref = (added.details as any).job_ref as string;
	const leadId = ((added.details as any).pipeline?.added ?? [])[0] as string;
	assert.ok(ref && leadId, "custom job is in the index and the pipeline");

	faux.setResponses([
		fauxAssistantMessage([fauxToolCall("list_pipeline", {})], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxToolCall("update_job_status", { job_id: leadId, status: "applied" })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxText("Marked as applied.")]),
	]);
	const [listed] = await run("I applied to it.");
	assert.ok(((listed.details as any[]) ?? []).some((j) => j.job_id === leadId));
	assert.equal((await api.get<any>(`/api/v1/leads/${leadId}`)).status, "applied");

	faux.setResponses([
		fauxAssistantMessage([fauxToolCall("delete_jobs", { job_refs: [ref], scope: "both" })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxToolCall("reset_jobs", { scope: "all", confirm: "RESET" })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxText("Deleted and reset.")]),
	]);
	const [deleted, reset] = await run("Delete that job and then reset everything — yes, I'm sure.");
	assert.equal((deleted.details as any).index_deleted, 1);
	assert.equal((deleted.details as any).pipeline_deleted, 1);
	assert.ok(reset);
	const status = await api.get<any>("/api/v1/jobindex/status");
	assert.equal(status.index.jobs_total, 0);

	// A reset without the confirmation word is refused by the API.
	await assert.rejects(api.post("/api/v1/jobindex/reset", { scope: "pipeline", confirm: "yes" }), /RESET/);

	await host.deleteSession(id);
	host.dispose();
	scheduler.stop();
});
