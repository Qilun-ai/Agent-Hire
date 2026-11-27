import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

export interface JobPreferences {
	target_roles?: string[];
	levels?: string[];
	locations?: string[];
	remote?: boolean;
	since_hours?: number;
	notes?: string;
	updated_at?: string;
}

/** The user's job-search preferences, shared by every chat and scheduled check. */
export class Preferences {
	private readonly file: string;

	constructor(dataDir: string) {
		this.file = join(dataDir, "preferences.json");
	}

	async get(): Promise<JobPreferences> {
		try {
			return JSON.parse(await readFile(this.file, "utf8"));
		} catch {
			return {};
		}
	}

	async update(patch: JobPreferences): Promise<JobPreferences> {
		const current = await this.get();
		const next: JobPreferences = { ...current };
		for (const [key, value] of Object.entries(patch)) {
			if (value !== undefined) (next as Record<string, unknown>)[key] = value;
		}
		next.updated_at = new Date().toISOString();
		await writeFile(this.file, JSON.stringify(next, null, 1), { mode: 0o600 });
		return next;
	}
}
