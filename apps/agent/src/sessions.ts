import {
	createAgentSession,
	createExtensionRuntime,
	SessionManager,
	SettingsManager,
	type AgentSession,
	type AgentSessionEvent,
	type ResourceLoader,
} from "@earendil-works/pi-coding-agent";
import { mkdirSync } from "node:fs";
import { rm } from "node:fs/promises";
import { join } from "node:path";
import { buildTools, type ToolContext } from "./tools/index.js";
import { systemPrompt } from "./prompt.js";
import { LlmManager, LlmNotConfigured } from "./llm.js";
import type { Schedule } from "./scheduler.js";

/** One event as the browser sees it. `seq` is per-session and strictly increasing. */
export interface ChatEvent {
	seq: number;
	type: string;
	[key: string]: unknown;
}

type Listener = (event: ChatEvent) => void;

const BUFFER = 3000;
const IDLE_DISPOSE_MS = 30 * 60_000;

interface LiveChat {
	id: string;
	session: AgentSession;
	seq: number;
	buffer: ChatEvent[];
	listeners: Set<Listener>;
	lastUsed: number;
	unsubscribe: () => void;
	pendingSource?: "user" | "schedule";
}

export interface DisplayItem {
	kind: "user" | "assistant" | "tool";
	text?: string;
	source?: "user" | "schedule";
	id?: string;
	name?: string;
	label?: string;
	args?: unknown;
	result?: string;
	details?: unknown;
	isError?: boolean;
	timestamp?: number;
	error?: string;
}

/**
 * Hosts Pi agent sessions for the web UI (the DeepSeek-harness pattern: the host owns sessions and
 * an ordered event stream; browsers subscribe with a cursor and resync from the transcript when the
 * cursor has fallen out of the buffer).
 */
export class ChatHost {
	private live = new Map<string, LiveChat>();
	private loading = new Map<string, Promise<LiveChat>>();
	private hostListeners = new Set<(event: { type: string; [k: string]: unknown }) => void>();
	private readonly sessionsDir: string;
	private readonly cwd: string;
	private reaper: NodeJS.Timeout;

	constructor(
		private readonly dataDir: string,
		private readonly llm: LlmManager,
		private readonly toolContext: Omit<ToolContext, "sessionId">,
	) {
		this.sessionsDir = join(dataDir, "sessions");
		this.cwd = join(dataDir, "workspace");
		mkdirSync(this.sessionsDir, { recursive: true, mode: 0o700 });
		mkdirSync(this.cwd, { recursive: true, mode: 0o700 });
		this.reaper = setInterval(() => this.reap(), 60_000);
		this.reaper.unref();
	}

	onHostEvent(listener: (event: { type: string; [k: string]: unknown }) => void): () => void {
		this.hostListeners.add(listener);
		return () => this.hostListeners.delete(listener);
	}

	private emitHost(event: { type: string; [k: string]: unknown }): void {
		for (const listener of this.hostListeners) listener(event);
	}

	private resourceLoader(): ResourceLoader {
		return {
			getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
			getSkills: () => ({ skills: [], diagnostics: [] }),
			getPrompts: () => ({ prompts: [], diagnostics: [] }),
			getThemes: () => ({ themes: [], diagnostics: [] }),
			getAgentsFiles: () => ({ agentsFiles: [] }),
			getSystemPrompt: () => systemPrompt(),
			getSystemPromptSource: () => undefined,
			getAppendSystemPrompt: () => [],
			getAppendSystemPromptSources: () => [],
			extendResources: () => {},
			reload: async () => {},
		} as unknown as ResourceLoader;
	}

	async listSessions(): Promise<Array<{ id: string; title: string; updated_at: string; messages: number; running: boolean }>> {
		const infos = await SessionManager.list(this.cwd, this.sessionsDir);
		return infos
			.map((info) => ({
				id: info.id,
				title: info.name || info.firstMessage.replace(/\s+/g, " ").slice(0, 70) || "New chat",
				updated_at: info.modified.toISOString(),
				messages: info.messageCount,
				running: this.live.get(info.id)?.session.isStreaming ?? false,
			}))
			.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
	}

	async createSession(): Promise<string> {
		const chat = await this.open(undefined);
		this.emitHost({ type: "sessions_changed" });
		return chat.id;
	}

	async deleteSession(id: string): Promise<boolean> {
		const path = SessionManager.findById(this.cwd, id, this.sessionsDir);
		const chat = this.live.get(id);
		if (chat) {
			chat.unsubscribe();
			chat.session.dispose();
			this.live.delete(id);
		}
		await this.toolContext.scheduler.removeForSession(id);
		if (!path) return false;
		await rm(path, { force: true });
		this.emitHost({ type: "sessions_changed" });
		return true;
	}

	async exists(id: string): Promise<boolean> {
		return this.live.has(id) || Boolean(SessionManager.findById(this.cwd, id, this.sessionsDir));
	}

	private async open(id: string | undefined): Promise<LiveChat> {
		if (id && this.live.has(id)) {
			const chat = this.live.get(id)!;
			chat.lastUsed = Date.now();
			return chat;
		}
		if (id && this.loading.has(id)) return this.loading.get(id)!;
		const task = this.build(id);
		if (id) this.loading.set(id, task);
		try {
			return await task;
		} finally {
			if (id) this.loading.delete(id);
		}
	}

	private async build(id: string | undefined): Promise<LiveChat> {
		let sessionManager: SessionManager;
		if (id) {
			const path = SessionManager.findById(this.cwd, id, this.sessionsDir);
			if (!path) throw new Error(`No chat ${id}`);
			sessionManager = SessionManager.open(path, this.sessionsDir, this.cwd);
		} else {
			sessionManager = SessionManager.create(this.cwd, this.sessionsDir);
		}
		let sessionId = sessionManager.getSessionId();
		const tools = buildTools({ ...this.toolContext, sessionId: () => sessionId });
		let model;
		try {
			model = await this.llm.currentModel();
		} catch {
			model = undefined; // created anyway; prompting reports the configuration problem
		}
		const { session } = await createAgentSession({
			cwd: this.cwd,
			agentDir: join(this.dataDir, "pi"),
			modelRuntime: this.llm.modelRuntime,
			model,
			thinkingLevel: "off",
			resourceLoader: this.resourceLoader(),
			tools: tools.map((t) => t.name),
			customTools: tools,
			sessionManager,
			settingsManager: SettingsManager.inMemory({
				compaction: { enabled: true },
				retry: { enabled: true, maxRetries: 2 },
			}),
		});
		sessionId = session.sessionId;
		const chat: LiveChat = {
			id: sessionId,
			session,
			seq: 0,
			buffer: [],
			listeners: new Set(),
			lastUsed: Date.now(),
			unsubscribe: () => {},
		};
		chat.unsubscribe = session.subscribe((event) => this.translate(chat, event));
		this.live.set(sessionId, chat);
		return chat;
	}

	private push(chat: LiveChat, event: Omit<ChatEvent, "seq">): void {
		const full = { ...event, seq: ++chat.seq } as ChatEvent;
		chat.buffer.push(full);
		if (chat.buffer.length > BUFFER) chat.buffer.splice(0, chat.buffer.length - BUFFER);
		for (const listener of chat.listeners) listener(full);
	}

	private translate(chat: LiveChat, event: AgentSessionEvent): void {
		switch (event.type) {
			case "agent_start":
				this.push(chat, { type: "run_start" });
				this.emitHost({ type: "session_running", id: chat.id, running: true });
				break;
			case "agent_settled":
				this.push(chat, { type: "run_end", aborted: event.aborted });
				this.emitHost({ type: "session_running", id: chat.id, running: false });
				this.emitHost({ type: "sessions_changed" });
				break;
			case "message_start": {
				const message = event.message as { role: string; content?: unknown };
				if (message.role === "user") {
					this.push(chat, { type: "user", text: textOf(message.content), source: chat.pendingSource ?? "user" });
					chat.pendingSource = undefined;
				} else if (message.role === "assistant") {
					this.push(chat, { type: "assistant_start" });
				}
				break;
			}
			case "message_update": {
				const delta = event.assistantMessageEvent;
				if (delta.type === "text_delta") this.push(chat, { type: "text_delta", delta: delta.delta });
				else if (delta.type === "thinking_delta") this.push(chat, { type: "thinking_delta", delta: delta.delta });
				break;
			}
			case "message_end": {
				const message = event.message as { role: string; content?: unknown; stopReason?: string; errorMessage?: string };
				if (message.role === "assistant") {
					this.push(chat, {
						type: "assistant_end",
						text: textOf(message.content),
						stopReason: message.stopReason,
						error: message.stopReason === "error" ? friendlyError(message.errorMessage) : undefined,
					});
				}
				break;
			}
			case "tool_execution_start":
				this.push(chat, { type: "tool_start", id: event.toolCallId, name: event.toolName, args: event.args });
				break;
			case "tool_execution_end": {
				const result = event.result as { content?: unknown; details?: unknown } | undefined;
				this.push(chat, {
					type: "tool_end",
					id: event.toolCallId,
					name: event.toolName,
					isError: event.isError,
					result: textOf(result?.content).slice(0, 4000),
					details: clip(result?.details),
				});
				break;
			}
			case "compaction_start":
				this.push(chat, { type: "notice", text: "Summarising older messages to keep the conversation within the model's memory…" });
				break;
			case "session_info_changed":
				this.emitHost({ type: "sessions_changed" });
				break;
			default:
				break;
		}
	}

	/** Send a user message. Resolves when accepted (the run continues in the background). */
	async send(id: string, text: string, source: "user" | "schedule" = "user"): Promise<void> {
		const chat = await this.open(id);
		chat.lastUsed = Date.now();
		try {
			const model = await this.llm.currentModel();
			if (chat.session.model?.id !== model.id || chat.session.model?.baseUrl !== model.baseUrl) {
				await chat.session.setModel(model);
			}
		} catch (error) {
			const message = error instanceof LlmNotConfigured ? error.message : `AI provider error: ${(error as Error).message}`;
			this.push(chat, { type: "user", text, source });
			this.push(chat, { type: "error", text: message, code: "llm_not_configured" });
			throw new LlmNotConfigured(message);
		}
		if (!chat.session.sessionName && source === "user") {
			chat.session.setSessionName(text.replace(/\[Attached file[^\]]*\]/g, "").replace(/\s+/g, " ").trim().slice(0, 60) || "Résumé upload");
		}
		chat.pendingSource = source;
		if (chat.session.isStreaming) {
			await chat.session.prompt(text, { streamingBehavior: "followUp", expandPromptTemplates: false });
			this.push(chat, { type: "queued", text, source });
			return;
		}
		const run = chat.session.prompt(text, { expandPromptTemplates: false });
		run.catch((error: Error) => this.push(chat, { type: "error", text: friendlyError(error.message) }));
	}

	async abort(id: string): Promise<void> {
		const chat = this.live.get(id);
		if (chat?.session.isStreaming) await chat.session.abort();
	}

	/** Scheduled check-in: wake the session with the instruction the agent wrote for itself. */
	async wake(schedule: Schedule): Promise<void> {
		if (!(await this.exists(schedule.sessionId))) {
			await this.toolContext.scheduler.remove(schedule.id);
			return;
		}
		const recurring = schedule.repeatEveryMinutes ? ` (repeats every ${schedule.repeatEveryMinutes} min; id ${schedule.id})` : ` (id ${schedule.id})`;
		const text = `[Scheduled check-in${recurring}] ${schedule.instruction}`;
		try {
			await this.send(schedule.sessionId, text, "schedule");
			this.emitHost({ type: "notification", sessionId: schedule.sessionId, text: schedule.instruction.slice(0, 140) });
		} catch (error) {
			console.error("[agent] scheduled wake failed:", (error as Error).message);
		}
	}

	subscribe(id: string, after: number, listener: Listener): { unsubscribe: () => void; resync: boolean } {
		const chat = this.live.get(id);
		if (!chat) return { unsubscribe: () => {}, resync: false };
		const first = chat.buffer[0]?.seq ?? chat.seq + 1;
		const resync = after > 0 && after < first - 1;
		if (!resync) for (const event of chat.buffer) if (event.seq > after) listener(event);
		chat.listeners.add(listener);
		chat.lastUsed = Date.now();
		return { unsubscribe: () => chat.listeners.delete(listener), resync };
	}

	/** Full conversation for display, rebuilt from the persisted Pi session. */
	async transcript(id: string): Promise<{ id: string; title: string; seq: number; running: boolean; items: DisplayItem[] }> {
		const chat = await this.open(id);
		const items: DisplayItem[] = [];
		const toolIndex = new Map<string, DisplayItem>();
		for (const raw of chat.session.messages as unknown as Array<Record<string, any>>) {
			if (raw.role === "user") {
				const text = textOf(raw.content);
				items.push({ kind: "user", text, source: text.startsWith("[Scheduled check-in") ? "schedule" : "user", timestamp: raw.timestamp });
			} else if (raw.role === "assistant") {
				const blocks: any[] = Array.isArray(raw.content) ? raw.content : [];
				const text = blocks.filter((b) => b.type === "text").map((b) => b.text).join("");
				if (text.trim() || raw.stopReason === "error") {
					items.push({ kind: "assistant", text, timestamp: raw.timestamp, error: raw.stopReason === "error" ? friendlyError(raw.errorMessage) : undefined });
				}
				for (const block of blocks.filter((b) => b.type === "toolCall")) {
					const item: DisplayItem = { kind: "tool", id: block.id, name: block.name, args: block.arguments };
					toolIndex.set(block.id, item);
					items.push(item);
				}
			} else if (raw.role === "toolResult") {
				const item = toolIndex.get(raw.toolCallId);
				if (item) {
					item.result = textOf(raw.content).slice(0, 4000);
					item.details = clip(raw.details);
					item.isError = raw.isError;
				}
			}
		}
		return {
			id: chat.id,
			title: chat.session.sessionName || items.find((i) => i.kind === "user")?.text?.slice(0, 60) || "New chat",
			seq: chat.seq,
			running: chat.session.isStreaming,
			items,
		};
	}

	private reap(): void {
		const now = Date.now();
		for (const [id, chat] of this.live) {
			if (!chat.session.isStreaming && chat.listeners.size === 0 && now - chat.lastUsed > IDLE_DISPOSE_MS) {
				chat.unsubscribe();
				chat.session.dispose();
				this.live.delete(id);
			}
		}
	}

	dispose(): void {
		clearInterval(this.reaper);
		for (const chat of this.live.values()) {
			chat.unsubscribe();
			chat.session.dispose();
		}
		this.live.clear();
	}
}

function textOf(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.map((block: any) => (block?.type === "text" ? block.text : ""))
		.filter(Boolean)
		.join("");
}

/** Keep tool details small enough for the event stream (the UI renders cards from them). */
function clip(value: unknown): unknown {
	if (value === undefined) return undefined;
	const text = JSON.stringify(value);
	if (text.length <= 60_000) return value;
	return { truncated: true, preview: text.slice(0, 2000) };
}

function friendlyError(message: string | undefined): string {
	const text = message || "The AI provider returned an error.";
	if (/401|unauthor|invalid api key|incorrect api key/i.test(text)) return "The AI provider rejected the API key. Check it in Settings.";
	if (/429|rate limit|quota/i.test(text)) return "The AI provider is rate-limiting or out of quota. Try again shortly or switch model in Settings.";
	if (/ENOTFOUND|ECONNREFUSED|fetch failed|network/i.test(text)) return "Could not reach the AI provider. Check your connection and the provider URL in Settings.";
	return text.slice(0, 500);
}
