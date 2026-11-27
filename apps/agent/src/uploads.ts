import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join, extname } from "node:path";
import { randomBytes } from "node:crypto";

export interface UploadRecord {
	id: string;
	name: string;
	mime: string;
	size: number;
	path: string;
	uploaded_at: string;
}

const MAX_BYTES = 20 * 1024 * 1024;
const ALLOWED = new Set([".pdf", ".docx", ".doc", ".txt", ".md", ".tex", ".rtf", ".png", ".jpg", ".jpeg"]);

/** Files attached in chat. Kept on disk so the agent can import them later (and re-import after edits). */
export class Uploads {
	constructor(private readonly dir: string) {}

	private index(): string {
		return join(this.dir, "index.json");
	}

	private async all(): Promise<Record<string, UploadRecord>> {
		try {
			return JSON.parse(await readFile(this.index(), "utf8"));
		} catch {
			return {};
		}
	}

	async save(name: string, mime: string, bytes: Uint8Array): Promise<UploadRecord> {
		if (bytes.byteLength > MAX_BYTES) throw new Error("File too large (max 20 MB)");
		const ext = extname(name).toLowerCase();
		if (!ALLOWED.has(ext)) throw new Error(`Unsupported file type ${ext || "(none)"}; use PDF, DOCX, TXT or an image`);
		await mkdir(this.dir, { recursive: true, mode: 0o700 });
		const id = "up_" + randomBytes(6).toString("hex");
		const safe = name.replace(/[^\w.\- ]+/g, "_").slice(-120);
		const path = join(this.dir, `${id}-${safe}`);
		await writeFile(path, bytes, { mode: 0o600 });
		const record: UploadRecord = { id, name: safe, mime, size: bytes.byteLength, path, uploaded_at: new Date().toISOString() };
		const all = await this.all();
		all[id] = record;
		await writeFile(this.index(), JSON.stringify(all, null, 1), { mode: 0o600 });
		return record;
	}

	async get(id: string): Promise<UploadRecord | undefined> {
		return (await this.all())[id];
	}
}
