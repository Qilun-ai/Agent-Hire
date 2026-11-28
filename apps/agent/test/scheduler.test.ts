import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Scheduler, type Schedule } from "../src/scheduler.js";

function dir() {
	return mkdtempSync(join(tmpdir(), "gh-sched-"));
}

test("one-shot fires once and is removed; recurring reschedules", async () => {
	const fired: Schedule[] = [];
	const s = new Scheduler(dir(), async (x) => void fired.push(x));
	await s.add({ sessionId: "a", instruction: "once", delayMinutes: 5 });
	await s.add({ sessionId: "b", instruction: "every", delayMinutes: 5, repeatEveryMinutes: 60 });
	await s.tick(Date.now() + 60_000);
	assert.equal(fired.length, 0, "not due yet");
	await s.tick(Date.now() + 6 * 60_000);
	await new Promise((r) => setImmediate(r));
	assert.deepEqual(fired.map((f) => f.instruction).sort(), ["every", "once"]);
	const left = s.list();
	assert.equal(left.length, 1);
	assert.equal(left[0].instruction, "every");
	assert.ok(Date.parse(left[0].nextRunAt) > Date.now() + 60 * 60_000, "next run one interval later");
});

test("schedules survive a restart, and missed wake-ups fire once", async () => {
	const d = dir();
	const first = new Scheduler(d, async () => {});
	await first.add({ sessionId: "a", instruction: "check scrape", delayMinutes: 1, repeatEveryMinutes: 30 });
	const fired: string[] = [];
	const second = new Scheduler(d, async (x) => void fired.push(x.instruction));
	await second.load();
	assert.equal(second.list().length, 1);
	await second.tick(Date.now() + 10 * 60 * 60_000); // app was down for 10 hours
	await new Promise((r) => setImmediate(r));
	assert.deepEqual(fired, ["check scrape"], "fires once, not once per missed interval");
});

test("recurring interval has a 15 minute floor and sessions can be cleared", async () => {
	const s = new Scheduler(dir(), async () => {});
	const x = await s.add({ sessionId: "a", instruction: "spam", delayMinutes: 1, repeatEveryMinutes: 1 });
	assert.equal(x.repeatEveryMinutes, 15);
	await s.add({ sessionId: "b", instruction: "keep", delayMinutes: 1 });
	await s.removeForSession("a");
	assert.deepEqual(s.list().map((y) => y.sessionId), ["b"]);
});
