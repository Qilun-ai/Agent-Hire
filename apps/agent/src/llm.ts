import { createProvider, type Model, type Api } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import { anthropicMessagesApi } from "@earendil-works/pi-ai/api/anthropic-messages.lazy";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { join } from "node:path";
import type { ApiClient, LlmConfig } from "./api-client.js";

export const PROVIDER_ID = "agent_hire";

export class LlmNotConfigured extends Error {}

/**
 * The agent uses whatever AI provider the user chose in Settings (one place for keys). The API
 * resolves it; this turns it into a pi-ai provider registered on the Pi ModelRuntime. Settings can
 * change at any time, so the config is re-read (cheaply cached) before each run.
 */
export class LlmManager {
	private runtime!: ModelRuntime;
	private fingerprint = "";
	private model: Model<Api> | undefined;
	private fetchedAt = 0;
	private cached: LlmConfig | undefined;

	constructor(
		private readonly api: ApiClient,
		private readonly dataDir: string,
	) {}

	async init(): Promise<ModelRuntime> {
		this.runtime = await ModelRuntime.create({
			authPath: join(this.dataDir, "auth.json"),
			modelsPath: null,
			refreshOnCreate: false,
			allowModelNetwork: false,
		});
		return this.runtime;
	}

	get modelRuntime(): ModelRuntime {
		return this.runtime;
	}

	private async config(force = false): Promise<LlmConfig> {
		if (!force && this.cached && Date.now() - this.fetchedAt < 10_000) return this.cached;
		this.cached = await this.api.llmConfig();
		this.fetchedAt = Date.now();
		return this.cached;
	}

	/** Current model, rebuilding the provider when Settings changed. Throws LlmNotConfigured with a user-facing reason. */
	async currentModel(force = false): Promise<Model<Api>> {
		let cfg: LlmConfig;
		try {
			cfg = await this.config(force);
		} catch (error) {
			throw new LlmNotConfigured(`Could not read the AI provider settings: ${(error as Error).message}`);
		}
		if (cfg.error || !cfg.base_url || !cfg.model || !cfg.api) {
			throw new LlmNotConfigured(cfg.message || "No AI provider is configured. Open Settings and paste an API key.");
		}
		const fingerprint = JSON.stringify([cfg.provider, cfg.api, cfg.base_url, cfg.model, cfg.api_key]);
		if (fingerprint !== this.fingerprint || !this.model) {
			this.model = this.register(cfg);
			this.fingerprint = fingerprint;
		}
		return this.model;
	}

	describe(): { provider?: string; model?: string } {
		return { provider: this.cached?.provider, model: this.cached?.model };
	}

	private register(cfg: LlmConfig): Model<Api> {
		const anthropic = cfg.api === "anthropic-messages";
		const apiKey = cfg.api_key ?? "";
		const model = {
			id: cfg.model!,
			name: `${cfg.provider} · ${cfg.model}`,
			api: cfg.api!,
			provider: PROVIDER_ID,
			baseUrl: cfg.base_url!,
			reasoning: false,
			input: ["text", "image"],
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
			contextWindow: 128_000,
			maxTokens: 16_000,
			...(anthropic
				? {}
				: {
						compat: {
							// Most OpenAI-compatible gateways reject the newer request fields.
							supportsStore: false,
							supportsDeveloperRole: false,
							supportsReasoningEffort: false,
							maxTokensField: "max_tokens" as const,
						},
					}),
		} as Model<Api>;
		const provider = createProvider({
			id: PROVIDER_ID,
			name: "Agent-Hire (Settings)",
			baseUrl: cfg.base_url!,
			auth: {
				apiKey: {
					name: "Agent-Hire AI provider",
					resolve: async () => ({ auth: { apiKey }, source: "Agent-Hire Settings" }),
				},
			},
			models: [model],
			api: anthropic ? anthropicMessagesApi() : openAICompletionsApi(),
		});
		this.runtime.registerNativeProvider(provider);
		return model;
	}
}
