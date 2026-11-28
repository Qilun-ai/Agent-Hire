import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** Runtime configuration, all from the environment the launcher (scripts/start.sh) provides. */
export interface AgentConfig {
	/** Public port: the agent is the front door. It serves the UI, /agent/*, and proxies the rest to the API. */
	port: number;
	host: string;
	/** Internal URL of the Rust API (profile, jobs, tailoring, email, extension data). */
	apiUrl: string;
	/** Shared secret that unlocks /api/v1/agent-internal/* (resolved LLM credentials). */
	agentSecret: string;
	/** Where chat sessions, schedules, and uploads live. */
	dataDir: string;
	/** The built web UI (apps/web/dist). Empty or missing forwards page requests to the API instead. */
	webDist: string;
}

/**
 * The web UI next to this agent: apps/web/dist in the repository, and app/web/dist in an installed
 * package (both are ../../web/dist from the agent's dist/main.js).
 */
function defaultWebDist(): string {
	return resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "web", "dist");
}

function defaultDataDir(): string {
	const base = process.env.XDG_DATA_HOME || join(homedir(), ".local", "share");
	return join(base, "Agent-Hire", "agent");
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AgentConfig {
	const dataDir = env.AGENT_HIRE_AGENT_DIR || defaultDataDir();
	mkdirSync(dataDir, { recursive: true, mode: 0o700 });
	return {
		port: Number(env.APP_PORT || env.AGENT_PORT || 8000),
		host: "127.0.0.1",
		apiUrl: (env.AGENT_HIRE_API_URL || `http://127.0.0.1:${env.API_INTERNAL_PORT || 8010}`).replace(/\/$/, ""),
		agentSecret: env.AGENT_HIRE_AGENT_SECRET || "",
		dataDir,
		webDist: env.AGENT_HIRE_WEB_DIST || defaultWebDist(),
	};
}
