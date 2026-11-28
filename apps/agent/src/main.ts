import { join } from "node:path";
import { loadConfig } from "./config.js";
import { ApiClient } from "./api-client.js";
import { LlmManager } from "./llm.js";
import { Scheduler } from "./scheduler.js";
import { Uploads } from "./uploads.js";
import { Preferences } from "./preferences.js";
import { ChatHost } from "./sessions.js";
import { createAppServer } from "./server.js";

async function main(): Promise<void> {
	const config = loadConfig();
	if (!config.agentSecret) {
		console.warn("[agent] AGENT_HIRE_AGENT_SECRET is not set; the agent cannot read the AI provider settings. Start through scripts/start.sh.");
	}
	const api = new ApiClient(config);
	const llm = new LlmManager(api, config.dataDir);
	await llm.init();

	const uploads = new Uploads(join(config.dataDir, "uploads"));
	const preferences = new Preferences(config.dataDir);
	let host!: ChatHost;
	const scheduler = new Scheduler(config.dataDir, (schedule) => host.wake(schedule));
	await scheduler.load();
	host = new ChatHost(config.dataDir, llm, { api, scheduler, uploads, preferences });
	scheduler.start();

	const server = createAppServer({ config, api, host, scheduler, uploads, preferences, llm });
	server.listen(config.port, config.host, () => {
		console.log(`[agent] Agent-Hire listening on http://${config.host}:${config.port} (API ${config.apiUrl})`);
		console.log(`PORT:${config.port}`);
	});

	const shutdown = () => {
		scheduler.stop();
		host.dispose();
		server.close(() => process.exit(0));
		setTimeout(() => process.exit(0), 3000).unref();
	};
	process.on("SIGINT", shutdown);
	process.on("SIGTERM", shutdown);
}

main().catch((error) => {
	console.error("[agent] fatal:", error);
	process.exit(1);
});
