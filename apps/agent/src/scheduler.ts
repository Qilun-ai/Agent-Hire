import { readFile, writeFile, rename } from "node:fs/promises";
import { join } from "node:path";
import { randomBytes } from "node:crypto";

export interface Schedule {
	id: string;
	sessionId: string;
	instruction: string;
	nextRunAt: string;
	repeatEveryMinutes?: number;
	createdAt: string;
	lastRunAt?: string;
	runs: number;
}

export type WakeHandler = (schedule: Schedule) => Promise<void>;

const TICK_MS = 15_000;

/**
 * The agent's self-scheduling. A schedule wakes one chat session with an instruction the agent wrote
 * for itself ("check scrape progress and report new SWE jobs from the last 24h"). Schedules are saved
 * to disk, so a restart does not lose them; a wake-up that came due while the app was down fires once
 * on the next tick (recurring ones are not replayed once per missed interval).
 */
export class Scheduler {
	private schedules = new Map<string, Schedule>();
	private timer: NodeJS.Timeout | undefined;
	private running = new Set<string>();
	private readonly file: string;

	constructor(
		dataDir: string,
		private readonly onWake: WakeHandler,
	) {
		this.file = join(dataDir, "schedules.json");
	}

	async load(): Promise<void> {
		try {
			const rows = JSON.parse(await readFile(this.file, "utf8")) as Schedule[];
			for (const row of rows) this.schedules.set(row.id, row);
		} catch {
			// first run
		}
	}

	start(): void {
		this.timer ??= setInterval(() => void this.tick(), TICK_MS);
		this.timer.unref();
	}

	stop(): void {
		if (this.timer) clearInterval(this.timer);
		this.timer = undefined;
	}

	private async persist(): Promise<void> {
		const tmp = this.file + ".tmp";
		await writeFile(tmp, JSON.stringify([...this.schedules.values()], null, 1), { mode: 0o600 });
		await rename(tmp, this.file);
	}

	async add(input: { sessionId: string; instruction: string; delayMinutes: number; repeatEveryMinutes?: number }): Promise<Schedule> {
		const schedule: Schedule = {
			id: "sch_" + randomBytes(4).toString("hex"),
			sessionId: input.sessionId,
			instruction: input.instruction.slice(0, 2000),
			nextRunAt: new Date(Date.now() + Math.max(1, input.delayMinutes) * 60_000).toISOString(),
			repeatEveryMinutes: input.repeatEveryMinutes ? Math.max(15, input.repeatEveryMinutes) : undefined,
			createdAt: new Date().toISOString(),
			runs: 0,
		};
		this.schedules.set(schedule.id, schedule);
		await this.persist();
		return schedule;
	}

	async remove(id: string): Promise<boolean> {
		const existed = this.schedules.delete(id);
		if (existed) await this.persist();
		return existed;
	}

	list(sessionId?: string): Schedule[] {
		return [...this.schedules.values()]
			.filter((s) => !sessionId || s.sessionId === sessionId)
			.sort((a, b) => a.nextRunAt.localeCompare(b.nextRunAt));
	}

	async removeForSession(sessionId: string): Promise<void> {
		let changed = false;
		for (const [id, s] of this.schedules) {
			if (s.sessionId === sessionId) {
				this.schedules.delete(id);
				changed = true;
			}
		}
		if (changed) await this.persist();
	}

	/** Exposed for tests. */
	async tick(now = Date.now()): Promise<void> {
		const due = [...this.schedules.values()].filter((s) => Date.parse(s.nextRunAt) <= now && !this.running.has(s.id));
		for (const schedule of due) {
			this.running.add(schedule.id);
			schedule.lastRunAt = new Date(now).toISOString();
			schedule.runs += 1;
			if (schedule.repeatEveryMinutes) {
				schedule.nextRunAt = new Date(now + schedule.repeatEveryMinutes * 60_000).toISOString();
			} else {
				this.schedules.delete(schedule.id);
			}
			await this.persist();
			this.onWake(schedule)
				.catch((error) => console.error(`[scheduler] wake ${schedule.id} failed:`, error))
				.finally(() => this.running.delete(schedule.id));
		}
	}
}
