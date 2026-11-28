/**
 * End-to-end agent flow against a RUNNING Agent-Hire API, with a scripted (faux) model.
 *
 * It exercises the real Pi session, real tools, real HTTP calls, the event stream, the persisted
 * transcript, and the scheduler waking the same chat. Only the model's choices are scripted.
 *
 *   AGENT_HIRE_API_URL=http://127.0.0.1:8310 npm test
 *
 * Skipped when no API is reachable.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
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

/** The lead id the API gives a promoted index job (mirrors `lead_id_for` in the Rust job index). */
function leadIdFor(ref: string): string {
	return /^[A-Za-z0-9_-]{1,128}$/.test(ref) ? ref : `job-${createHash("sha256").update(ref).digest("hex").slice(0, 20)}`;
}

async function apiUp(): Promise<boolean> {
	try {
		return (await fetch(`${API}/health`, { signal: AbortSignal.timeout(2000) })).ok;
	} catch {
		return false;
	}
}

function waitFor(events: ChatEvent[], predicate: (e: ChatEvent) => boolean, ms = 120_000): Promise<ChatEvent> {
	return new Promise((resolve, reject) => {
		const started = Date.now();
		const timer = setInterval(() => {
			const hit = events.find(predicate);
			if (hit) {
				clearInterval(timer);
				resolve(hit);
			} else if (Date.now() - started > ms) {
				clearInterval(timer);
				reject(new Error(`timed out; saw: ${events.map((e) => e.type).join(",")}`));
			}
		}, 50);
	});
}

test("agent: status → search → shortlist → schedule → wake", { timeout: 300_000 }, async (t) => {
	if (!(await apiUp())) return t.skip(`no API at ${API}`);
	const dir = mkdtempSync(join(tmpdir(), "gh-agent-test-"));
	const api = new ApiClient({ port: 0, host: "127.0.0.1", apiUrl: API, agentSecret: "", dataDir: dir, webDist: "" });

	const runtime = await ModelRuntime.create({ authPath: join(dir, "auth.json"), modelsPath: null, refreshOnCreate: false });
	const faux = fauxProvider({ provider: "faux" });
	runtime.registerNativeProvider(faux.provider);
	const model = faux.getModel();
	const llm = { modelRuntime: runtime, currentModel: async () => model, describe: () => ({ provider: "faux", model: model.id }) } as unknown as LlmManager;

	let host!: ChatHost;
	const scheduler = new Scheduler(dir, (s) => host.wake(s));
	const preferences = new Preferences(dir);
	host = new ChatHost(dir, llm, { api, scheduler, uploads: new Uploads(join(dir, "uploads")), preferences });

	// Pick real jobs to shortlist so promote works against the live index.
	const found = await api.post<{ jobs: { job_ref: string; in_pipeline: boolean }[]; total_matches: number }>("/api/v1/jobindex/search", {
		role: "software engineer", levels: ["junior"], since_hours: 24 * 30, page_size: 25,
	});
	assert.ok(found.total_matches > 0, "index should contain junior software jobs");
	const refs = found.jobs.filter((j) => !j.in_pipeline).slice(0, 2).map((j) => j.job_ref);
	assert.equal(refs.length, 2, "two jobs not yet in the pipeline");

	faux.setResponses([
		fauxAssistantMessage([fauxToolCall("job_index_status", {})], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxToolCall("set_preferences", { target_roles: ["software engineer"], levels: ["junior"], since_hours: 720 })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxToolCall("search_jobs", { role: "software engineer", levels: ["junior"], since_hours: 720, page_size: 5 })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxToolCall("shortlist_jobs", { job_refs: refs, note: "test shortlist" })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxToolCall("schedule_check", { instruction: "Check scrape progress and report new junior SWE jobs", delay_minutes: 1 })], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxText("Found junior software jobs, shortlisted 2, and I'll check back soon.")]),
	]);

	const id = await host.createSession();
	const events: ChatEvent[] = [];
	host.subscribe(id, 0, (e) => events.push(e));
	await host.send(id, "I'm a fresher looking for software engineering roles from the last month.");
	await waitFor(events, (e) => e.type === "run_end");

	const toolEnds = events.filter((e) => e.type === "tool_end");
	assert.deepEqual(toolEnds.map((e) => e.name), ["job_index_status", "set_preferences", "search_jobs", "shortlist_jobs", "schedule_check"]);
	for (const e of toolEnds) assert.equal(e.isError, false, `${e.name} failed: ${e.result}`);
	const search = toolEnds.find((e) => e.name === "search_jobs")!;
	assert.ok(((search.details as any).total_matches ?? 0) > 0, "search returned matches");
	assert.ok(!String(search.result).includes('"index"'), "index stats stripped from the model's view");
	const shortlist = toolEnds.find((e) => e.name === "shortlist_jobs")!.details as any;
	assert.equal(shortlist.added.length + shortlist.already_in_pipeline.length, 2);
	const final = events.filter((e) => e.type === "assistant_end").pop()!;
	assert.match(String(final.text), /shortlisted 2/);
	assert.deepEqual((await preferences.get()).levels, ["junior"]);

	// Persisted transcript rebuilds the same conversation.
	const transcript = await host.transcript(id);
	assert.equal(transcript.items.filter((i) => i.kind === "tool").length, 5);
	assert.ok(transcript.items.every((i) => i.kind !== "tool" || i.result !== undefined), "every tool has its result");

	// Leads now exist in the pipeline and the extension queue.
	const ext = await api.get<{ jobs?: { job_id: string }[] }>("/api/v1/extension/jobs");
	const extIds = (ext.jobs ?? []).map((j) => j.job_id);
	for (const ref of refs) assert.ok(extIds.includes(leadIdFor(ref)), `extension queue has ${ref}`);

	// The schedule fires: the agent wakes in the same chat and acts on its own instruction.
	const schedules = scheduler.list(id);
	assert.equal(schedules.length, 1);
	faux.setResponses([
		fauxAssistantMessage([fauxToolCall("scrape_status", {})], { stopReason: "toolUse" }),
		fauxAssistantMessage([fauxText("Check-in: no scrape is running; nothing new yet.")]),
	]);
	const before = events.length;
	const lastSeq = events[before - 1]?.seq ?? 0;
	await scheduler.tick(Date.now() + 2 * 60_000);
	await waitFor(events, (e) => e.type === "run_end" && (e.seq ?? 0) > lastSeq);
	const wake = events.slice(before).find((e) => e.type === "user");
	assert.ok(wake && wake.source === "schedule" && String(wake.text).startsWith("[Scheduled check-in"), "woke with the scheduled instruction");
	assert.ok(events.slice(before).some((e) => e.type === "tool_end" && e.name === "scrape_status" && !e.isError));
	assert.equal(scheduler.list(id).length, 0, "one-shot schedule removed after firing");

	// Reconnect semantics: a cursor inside the buffer replays only newer events.
	const replay: ChatEvent[] = [];
	const sub = host.subscribe(id, lastSeq, (e) => replay.push(e));
	sub.unsubscribe();
	assert.equal(replay.length, events.length - before);
	assert.equal(sub.resync, false);

	await host.deleteSession(id);
	host.dispose();
	scheduler.stop();
});
