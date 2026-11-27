import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type, type TSchema } from "typebox";
import { readFile, stat } from "node:fs/promises";
import { basename } from "node:path";
import type { ApiClient } from "../api-client.js";
import type { Scheduler } from "../scheduler.js";
import type { Uploads } from "../uploads.js";
import type { Preferences } from "../preferences.js";

export interface ToolContext {
	api: ApiClient;
	scheduler: Scheduler;
	uploads: Uploads;
	preferences: Preferences;
	/** The chat session the tool runs in (schedules wake this session). */
	sessionId: () => string;
}

type Json = Record<string, unknown>;

function ok(summary: string, details: unknown = {}) {
	return { content: [{ type: "text" as const, text: summary }], details };
}

/** Compact JSON for the model: drops null/empty fields so results cost fewer tokens. */
function compact(value: unknown): string {
	return JSON.stringify(value, (_key, v) => {
		if (v === null || v === undefined || v === "") return undefined;
		if (Array.isArray(v) && v.length === 0) return undefined;
		return v;
	});
}

function define<T extends TSchema>(tool: Omit<ToolDefinition<T>, "execute"> & { execute: (params: any, signal?: AbortSignal) => Promise<any> }): ToolDefinition {
	return {
		...tool,
		execute: (_id: string, params: unknown, signal?: AbortSignal) => tool.execute(params, signal),
	} as unknown as ToolDefinition;
}

const levelEnum = Type.Union(["intern", "fresher", "junior", "mid", "senior", "lead"].map((l) => Type.Literal(l)));

export function buildTools(ctx: ToolContext): ToolDefinition[] {
	const { api } = ctx;

	const profileSummary = async () => {
		const p = (await api.get<Json>("/api/v1/profile")) ?? {};
		const list = (key: string) => (Array.isArray(p[key]) ? (p[key] as Json[]) : []);
		const skills = list("skills").map((s) => String(s.n ?? s.name ?? "")).filter(Boolean);
		return {
			name: p.n || "",
			summary: String(p.s || "").slice(0, 400),
			skills_count: skills.length,
			skills: skills.slice(0, 80),
			skill_ids: list("skills").map((x) => ({ id: x.id, name: x.n ?? x.name, category: x.cat ?? x.category })),
			experience: list("exp").map((e) => ({ id: e.id, role: e.role, company: e.co ?? e.company, period: e.period ?? e.dates, bullets: String(e.d ?? e.description ?? "").slice(0, 600) })),
			projects: list("projects").map((x) => ({ id: x.id, title: x.title ?? x.name, stack: x.stack, impact: String(x.impact ?? "").slice(0, 300) })),
			education: list("education").slice(0, 4),
			certifications: list("certifications").slice(0, 12),
			achievements: list("achievements").slice(0, 12),
			identity: p.identity,
		};
	};

	return [
		define({
			name: "get_profile",
			label: "Read profile",
			description:
				"Read the user's saved profile (name, skills, experience, projects, education). Use before searching or tailoring, and to check whether a résumé has been imported.",
			parameters: Type.Object({}),
			execute: async () => {
				const summary = await profileSummary();
				const prefs = await ctx.preferences.get();
				return ok(compact({ profile: summary, preferences: prefs }), { profile: summary, preferences: prefs });
			},
		}),

		define({
			name: "import_document",
			label: "Import document",
			description: [
				"Save a file the user attached in chat (use the upload_id from the [Attached file …] note) to their documents, and get its text back so YOU can read it.",
				"kind='resume' for a résumé/CV, 'cover_letter' for cover letters and other reference writing.",
				"There is no automatic parser: after reading a résumé, extract the details yourself and call save_profile.",
				"Long documents come back in parts — call read_document with the next offset for the rest.",
			].join(" "),
			parameters: Type.Object({
				upload_id: Type.String({ description: "The upload_id from the [Attached file ...] note" }),
				kind: Type.Union([Type.Literal("resume"), Type.Literal("cover_letter")]),
			}),
			execute: async ({ upload_id, kind }) => {
				const file = await ctx.uploads.get(upload_id);
				if (!file) throw new Error(`No uploaded file with id ${upload_id}`);
				const bytes = await readFile(file.path);
				const form = new FormData();
				form.set("kind", kind);
				form.set("ingest", "false"); // store only — the agent reads it itself
				form.set("file", new Blob([bytes], { type: file.mime || "application/octet-stream" }), file.name);
				const saved = await api.request<Json>("POST", "/api/v1/documents", undefined, { form, timeoutMs: 120_000 });
				return documentResult(await extractText(api, file.name, file.mime, bytes), 0, {
					saved_as: kind, document_id: saved.id ?? saved.doc_id, file: file.name,
					next_step: kind === "resume" ? "Read the résumé and call save_profile with everything you extract." : "Keep this in mind as reference for tailoring and cover letters.",
				});
			},
		}),

		define({
			name: "read_document",
			label: "Read document",
			description: "Read (or keep reading) an uploaded document's text. Use offset to page through long documents.",
			parameters: Type.Object({
				upload_id: Type.String(),
				offset: Type.Optional(Type.Number({ description: "Character offset to continue from (from the previous result)" })),
			}),
			execute: async ({ upload_id, offset }) => {
				const file = await ctx.uploads.get(upload_id);
				if (!file) throw new Error(`No uploaded file with id ${upload_id}`);
				const bytes = await readFile(file.path);
				return documentResult(await extractText(api, file.name, file.mime, bytes), Math.max(0, Math.floor(offset ?? 0)), { file: file.name });
			},
		}),

		define({
			name: "read_web_page",
			label: "Read web page",
			description: [
				"Open a web page the user gives you (a job posting, a company's careers page) and read its text.",
				"Use it whenever the user shares a job link: read the posting first, then pass its full text as add_custom_job's description",
				"(and the title, company and location it shows). Long pages come back in parts: call again with the next offset.",
			].join(" "),
			parameters: Type.Object({
				url: Type.String({ description: "Full http(s) link" }),
				offset: Type.Optional(Type.Number({ description: "Character offset to continue from (from the previous result)" })),
			}),
			execute: async ({ url, offset }) => {
				const page = await api.request<Json>("POST", "/api/v1/agent-docs/read-page", { url }, { timeoutMs: 60_000 });
				const text = String(page.text ?? "");
				const start = Math.max(0, Math.floor(offset ?? 0));
				const chunk = text.slice(start, start + 12_000);
				const next = start + chunk.length < text.length ? start + chunk.length : null;
				const header = {
					url: page.url, title: page.title, company: page.company ?? null, location: page.location ?? null,
					date_posted: page.date_posted ?? null, source: page.source, total_chars: text.length,
					showing: `${start}-${start + chunk.length}`, next_offset: next,
					...(next === null && page.truncated ? { note: "The page was very long; only its first part was read." } : {}),
				};
				return ok(`${compact(header)}\n\n${chunk || "(no readable text on this page)"}`, { ...header, preview: chunk.slice(0, 600) });
			},
		}),

		define({
			name: "save_application_answers",
			label: "Save application answers",
			description: [
				"Save the answers application forms ask for (Profile → Application answers). The browser extension fills forms from these,",
				"NOT from preferences notes — so whenever the user's résumé or messages state them, save them here too:",
				"work authorization, sponsorship, notice period, salary expectation, current CTC, relocation, languages, education, etc.",
				"Keys (facts): full_name, first_name, last_name, preferred_name, pronouns, email, phone, phone_country_code, address_line1, city, state, postal_code, country, linkedin_url, github_url, portfolio_url, citizenship, country_of_residence, work_authorized, needs_sponsorship, visa_status, over_18, willing_to_relocate, notice_period, earliest_start_date, salary_expectation, current_ctc, relative_at_company, previously_employed_here, referral, how_heard, veteran_status, military_service, gender, race_ethnicity, disability_status, sexual_orientation, languages, highest_education. Keys (defaults): total_years_experience, current_title, current_company, summary, why_interested.",
				"Values are short strings (e.g. work_authorized 'Yes', needs_sponsorship 'No', notice_period '30 days'). Merges with what is saved.",
			].join(" "),
			parameters: Type.Object({
				answers: Type.Record(Type.String(), Type.String(), { description: "key → answer, using the keys listed above" }),
			}),
			execute: async ({ answers }) => {
				const current = await api.get<Json>("/api/v1/fill/facts");
				const schema = (current.schema ?? {}) as { facts?: Array<{ key: string }>; defaults?: Array<{ key: string }> };
				const factKeys = new Set((schema.facts ?? []).map((f) => f.key));
				const defaultKeys = new Set((schema.defaults ?? []).map((f) => f.key));
				const facts = { ...((current.facts ?? {}) as Record<string, string>) };
				const defaults = { ...((current.defaults ?? {}) as Record<string, string>) };
				const saved: string[] = [], unknown: string[] = [];
				for (const [key, value] of Object.entries(answers ?? {})) {
					if (factKeys.has(key)) { facts[key] = String(value); saved.push(key); }
					else if (defaultKeys.has(key)) { defaults[key] = String(value); saved.push(key); }
					else unknown.push(key);
				}
				await api.request<Json>("PUT", "/api/v1/fill/facts", { facts, defaults });
				return ok(compact({ saved, unknown_keys: unknown }), { saved, unknown });
			},
		}),

		define({
			name: "save_profile",
			label: "Save profile",
			description: [
				"Save the profile YOU extracted from the user's résumé (or from what they tell you). Fill every section you can;",
				"keep wording faithful to the source — do not invent employers, dates, metrics or skills.",
				"experience[].description: the role's bullet points, one per line, each starting with '- '. projects[].impact: what it does/achieved; projects[].stack: comma-separated technologies.",
				"skills: every concrete skill (languages, frameworks, tools, platforms) with a category such as language, frontend, backend, data, ml, cloud, devops, tool, soft.",
				"Saving merges into the existing profile. Then also call save_application_answers with the form answers the résumé states (work authorization, notice period, salary, …).",
			].join(" "),
			parameters: Type.Object({
				candidate: Type.Optional(Type.Object({ name: Type.Optional(Type.String()), summary: Type.Optional(Type.String()) })),
				identity: Type.Optional(Type.Object({
					email: Type.Optional(Type.String()), phone: Type.Optional(Type.String()),
					linkedin_url: Type.Optional(Type.String()), github_url: Type.Optional(Type.String()),
					website_url: Type.Optional(Type.String()), city: Type.Optional(Type.String()),
				})),
				skills: Type.Optional(Type.Array(Type.Object({ name: Type.String(), category: Type.Optional(Type.String()) }))),
				experience: Type.Optional(Type.Array(Type.Object({
					role: Type.String(), company: Type.String(), period: Type.Optional(Type.String()), description: Type.Optional(Type.String()),
				}))),
				projects: Type.Optional(Type.Array(Type.Object({
					title: Type.String(), stack: Type.Optional(Type.String()), repo: Type.Optional(Type.String()), impact: Type.Optional(Type.String()),
				}))),
				education: Type.Optional(Type.Array(Type.Object({ title: Type.String() }))),
				certifications: Type.Optional(Type.Array(Type.Object({ title: Type.String() }))),
				achievements: Type.Optional(Type.Array(Type.Object({ title: Type.String() }))),
			}),
			execute: async (profile) => {
				const result = await api.post<Json>("/api/v1/ingest/profile", profile, 180_000);
				const summary = await profileSummary();
				const out = { status: result.status, saved: result.stats, problems: result.errors, profile: summary };
				return ok(compact(out), out);
			},
		}),

		define({
			name: "set_preferences",
			label: "Save job preferences",
			description:
				"Remember the user's job-search preferences (target roles, levels, locations, remote, posting window). Later searches and scheduled checks use them as defaults.",
			parameters: Type.Object({
				target_roles: Type.Optional(Type.Array(Type.String(), { description: "e.g. ['software engineer','backend engineer']" })),
				levels: Type.Optional(Type.Array(levelEnum)),
				locations: Type.Optional(Type.Array(Type.String())),
				remote: Type.Optional(Type.Boolean()),
				since_hours: Type.Optional(Type.Number({ description: "Only jobs posted within this many hours, e.g. 24" })),
				notes: Type.Optional(Type.String({ description: "Anything else to remember (visa, salary, companies to avoid)" })),
			}),
			execute: async (params) => {
				const saved = await ctx.preferences.update(params);
				return ok(compact({ saved }), saved);
			},
		}),

		define({
			name: "job_index_status",
			label: "Job index status",
			description:
				"How many jobs are indexed, how many are from the last 24h, which companies are covered, and the state of any running scrape. Call before searching to know whether scraping is needed.",
			parameters: Type.Object({}),
			execute: async () => {
				const [s, c] = await Promise.all([
					api.get<Json>("/api/v1/jobindex/status"),
					api.get<Json>("/api/v1/jobindex/companies").catch(() => ({}) as Json),
				]);
				const categories = ((c.categories ?? []) as Json[]).map((x) => ({
					category: x.key, label: x.label, per_company: x.range, companies: x.count,
					est_minutes: x.est_wall_seconds == null ? null : Math.round(Number(x.est_wall_seconds) / 60),
					indexed_jobs: x.jobs,
				}));
				const view = { ...s, speed_categories: categories };
				return ok(compact(view), view);
			},
		}),

		define({
			name: "start_scrape",
			label: "Start scraping",
			description:
				"Start a background scrape of company career sites: all 237 verified companies by default, a speed category, or named companies. Companies are grouped by how long one scrape takes (fast < 1 min, quick 1–5 min, medium 5–15 min, slow 15–60 min, very_slow > 1 h; see job_index_status). Runs always go fastest-first, so the first jobs land within minutes even in a full pass that takes hours. When the user wants results soon, offer the fast (and quick) categories first. Returns immediately — follow up with schedule_check to report progress later. Do not start one when a scrape is already running.",
			parameters: Type.Object({
				companies: Type.Optional(Type.Array(Type.String(), { description: "Company names or slugs" })),
				categories: Type.Optional(Type.Array(Type.Union(["fast", "quick", "medium", "slow", "very_slow"].map((c) => Type.Literal(c))), { description: "Speed categories to scrape" })),
				confirmed_by_user: Type.Optional(Type.Boolean({
					description: "Set true ONLY when the user explicitly asked for a scrape in this conversation (or said yes when you asked). Required for a full scrape, any speed category, or more than 10 companies.",
				})),
			}),
			execute: async ({ companies, categories, confirmed_by_user }) => {
				const big = !(companies ?? []).length || (categories ?? []).length > 0 || (companies ?? []).length > 10;
				if (big && !confirmed_by_user) {
					// A full or category scrape runs for hours against hundreds of career sites: never start one on a guess.
					return ok("Not started: a scrape of this size needs the user's go-ahead. Ask the user whether to start it (it can take hours), then call again with confirmed_by_user=true. To look at one job the user linked, use read_web_page instead.",
						{ started: false, reason: "needs_user_confirmation" });
				}
				const r = await api.post<Json>("/api/v1/jobindex/scrape", { companies: companies ?? [], categories: categories ?? [] });
				return ok(compact(r), r);
			},
		}),

		define({
			name: "scrape_status",
			label: "Scrape progress",
			description: "Progress of the running scrape and the last few runs (companies done, new jobs found).",
			parameters: Type.Object({}),
			execute: async () => {
				const r = await api.get<Json>("/api/v1/jobindex/scrape");
				return ok(compact(r), r);
			},
		}),

		define({
			name: "search_jobs",
			label: "Search jobs",
			description: [
				"Search the scraped job index. Filters run in this order: posting window → role (decision model if configured, else rules) → level → profile skill overlap.",
				"Returns a COUNT plus ONE compact page (no descriptions). Page through with `page` instead of asking for huge pages.",
				"match_profile=true keeps only jobs mentioning at least one of the user's skills (ANY skill, not all) and ranks by how many match.",
				"posted_basis tells you whether 'posted' is the company's published date or when Agent-Hire first saw the job.",
				"departments filters by the built-in department model (38 subcategories in 14 departments; each job shows `department`). Pass codes or names: Technology = ENG (hardware/mechanical/electrical engineering), SWE (software development, incl. cloud/security/network/infrastructure engineers and system/network/database admins), IT (service desk, user technical support, ServiceNow/SAP/Salesforce/ERP admins and consultants), DATA (data and analytics); others e.g. PM, PGM, DES, SALES, MKT, FIN, HR, LEGAL, OPS, CS, QA, RND, SCM. A department name like 'Technology' or 'Healthcare' selects all its subcategories. facets.departments counts the matches by subcategory.",
				"level_source: 'encoder' = the built-in level model (decides first; reads title, requirements and duties); 'stated' = the posting's own stated years or title, used where the model has no confident verdict; 'kaggle'/'decision_api' = the decision model's estimate.",
			].join(" "),
			parameters: Type.Object({
				role: Type.Optional(Type.String({ description: "Target role, e.g. 'software engineer'" })),
				levels: Type.Optional(Type.Array(levelEnum, { description: "fresher = no experience required (0 years / new grad); junior = 1-2 years; mid = 3-4; senior = 5+; lead = staff/principal/manager+" })),
				departments: Type.Optional(Type.Array(Type.String(), { description: "Department subcategory codes or names, e.g. ['SWE'], ['IT'], ['Technology'], ['Product Management']" })),
				since_hours: Type.Optional(Type.Number({ description: "Posted within N hours (24 = last day)" })),
				locations: Type.Optional(Type.Array(Type.String())),
				remote: Type.Optional(Type.Boolean()),
				companies: Type.Optional(Type.Array(Type.String())),
				keywords: Type.Optional(Type.Array(Type.String(), { description: "Terms matched against job titles and extracted skills" })),
				match_profile: Type.Optional(Type.Boolean()),
				sort: Type.Optional(Type.Union([Type.Literal("match"), Type.Literal("recent"), Type.Literal("level")], { description: "level = most senior first (lead, senior, mid, junior, fresher, intern)" })),
				page: Type.Optional(Type.Number()),
				page_size: Type.Optional(Type.Number({ description: "Max 25" })),
			}),
			execute: async (params) => {
				const prefs = await ctx.preferences.get();
				const body = {
					role: params.role ?? prefs.target_roles?.[0] ?? "",
					levels: params.levels ?? prefs.levels ?? [],
					departments: params.departments ?? [],
					since_hours: params.since_hours ?? null,
					locations: params.locations ?? prefs.locations ?? [],
					remote: params.remote ?? null,
					companies: params.companies ?? [],
					keywords: params.keywords ?? [],
					match_profile: params.match_profile ?? false,
					sort: params.sort ?? "match",
					page: Math.max(1, Math.floor(params.page ?? 1)),
					page_size: Math.min(25, Math.max(1, Math.floor(params.page_size ?? 15))),
				};
				const r = await api.post<Json>("/api/v1/jobindex/search", body, 600_000);
				const { index, ...rest } = r;
				return ok(compact(rest), r);
			},
		}),

		define({
			name: "get_job",
			label: "Job details",
			description: "Full details of one job (description, skills, apply URL). Use sparingly — descriptions are long.",
			parameters: Type.Object({ job_ref: Type.String() }),
			execute: async ({ job_ref }) => {
				const j = await api.get<Json>(`/api/v1/jobindex/jobs/${encodeURIComponent(job_ref)}`);
				const view = {
					job_ref: j.id, title: j.title, company: j.company, location: j.location_text, workplace: j.workplace_type,
					level: j.rule_level, min_years: j.min_years, department: (j.department_model as { name?: string } | null | undefined)?.name ?? null, posted: j.date_posted ?? (j.baseline ? null : j.first_seen_at),
					skills: j.skills, url: j.apply_url ?? j.url, salary: j.salary,
					description: String(j.description ?? "").slice(0, 5000),
					in_pipeline: Boolean(j.lead_id),
				};
				return ok(compact(view), j);
			},
		}),

		define({
			name: "shortlist_jobs",
			label: "Shortlist jobs",
			description:
				"Add chosen jobs to the user's application pipeline (Applications tab) and the browser-extension queue. Use job_ref values from search_jobs. Returns the pipeline job ids used by tailor_resume.",
			parameters: Type.Object({
				job_refs: Type.Array(Type.String(), { minItems: 1, maxItems: 50 }),
				note: Type.Optional(Type.String({ description: "Why these were chosen (shown to the user)" })),
			}),
			execute: async ({ job_refs, note }) => {
				const r = await api.post<Json>("/api/v1/jobindex/promote", { job_refs, add_to_cart: true, note: note ?? "" });
				return ok(compact(r), r);
			},
		}),

		define({
			name: "tailor_resume",
			label: "Tailor résumé",
			description:
				"Build a job-specific résumé for one pipeline job: the AI selects the most relevant experience, projects and bullet points for that job description (so a long profile fits on the page), then renders the PDF. Takes up to a few minutes per job.",
			parameters: Type.Object({
				job_id: Type.String({ description: "Pipeline job id from shortlist_jobs" }),
			}),
			executionMode: "sequential",
			execute: async ({ job_id }) => {
				const id = encodeURIComponent(job_id);
				let selection: Json | undefined;
				let selectionNote = "";
				try {
					const s = await api.post<Json>(`/api/v1/leads/${id}/suggest-selection`, {}, 300_000);
					const keys = ["sections", "experience_order", "experience_on", "projects_order", "projects_on", "points_on", "skills_on"];
					const picked = (s.selection ?? s) as Json;
					selection = { version: 1 };
					for (const key of keys) if (Array.isArray(picked[key])) selection[key] = picked[key];
					selectionNote = `selected ${(selection.projects_on as unknown[] | undefined)?.length ?? 0} projects, ${(selection.points_on as unknown[] | undefined)?.length ?? 0} bullet points`;
				} catch (error) {
					selectionNote = `AI selection unavailable (${(error as Error).message.slice(0, 160)}); using the full profile`;
				}
				const r = await api.post<Json>(`/api/v1/leads/${id}/generate`, selection ? { selection } : {}, 600_000);
				const lead = (r.lead ?? {}) as Json;
				const out = {
					job_id,
					status: r.status,
					selection: selectionNote,
					resume_ready: Boolean(lead.resume_asset || lead.asset || lead.asset_path),
					title: lead.title,
					company: lead.company,
				};
				return ok(compact(out), r);
			},
		}),

		define({
			name: "extension_queue",
			label: "Extension queue",
			description: "Jobs queued for the browser extension, and whether each has a tailored résumé ready.",
			parameters: Type.Object({}),
			execute: async () => {
				const r = await api.get<Json>("/api/v1/extension/jobs");
				return ok(compact(r), r);
			},
		}),

		define({
			name: "application_summary",
			label: "Application tracker",
			description:
				"Counts of applications by stage (applied, interviewing, rejected, offers…) and recent status changes — the same data as the Applications & Chart tab.",
			parameters: Type.Object({}),
			execute: async () => {
				const [summary, email] = await Promise.all([
					api.get<Json>("/api/v1/leads/summary").catch((e) => ({ error: String(e.message) })),
					api.get<Json>("/api/v1/email/status").catch((e) => ({ error: String(e.message) })),
				]);
				return ok(compact({ pipeline: summary, email }), { summary, email });
			},
		}),

		define({
			name: "check_email",
			label: "Check email",
			description:
				"Scan the connected mailbox now for application confirmations, interview invites, rejections and offers, and update application stages. Requires email to be connected in Settings.",
			parameters: Type.Object({}),
			execute: async () => {
				const r = await api.post<Json>("/api/v1/email/poll?since_days=14", {}, 300_000);
				if (r.error) throw new Error(String(r.error));
				return ok(compact(r), r);
			},
		}),

		define({
			name: "classify_jobs",
			label: "Classify job levels",
			description: "Run the decision model (Kaggle notebook or decision API) over scraped software jobs that have no model verdict yet: role fit + intern/junior/mid/senior/lead. A background job does this continuously while Kaggle is ready; use this to push a batch now and to see how much is left.",
			parameters: Type.Object({ limit: Type.Optional(Type.Number({ description: "Jobs to classify now (max 400)" })) }),
			execute: async ({ limit }) => {
				const r = await api.post<Json>("/api/v1/jobindex/classify", { limit: Math.min(400, Math.max(1, Math.floor(limit ?? 96))) }, 900_000);
				return ok(compact(r), r);
			},
		}),

		define({
			name: "add_custom_job",
			label: "Add custom job",
			description: "Add a job the user gives you (title, company, description or pasted posting, link). If they give a link, read it with read_web_page first and pass the posting's full text as description. It goes into the job index and, by default, the Applications pipeline and extension queue.",
			parameters: Type.Object({
				title: Type.String(), company: Type.String(),
				description: Type.Optional(Type.String({ description: "The posting text, as complete as you have it" })),
				url: Type.Optional(Type.String()), location: Type.Optional(Type.String()),
				workplace_type: Type.Optional(Type.String({ description: "remote / hybrid / onsite" })),
				add_to_pipeline: Type.Optional(Type.Boolean()),
			}),
			execute: async (params) => {
				const r = await api.post<Json>("/api/v1/jobindex/custom", { add_to_pipeline: true, ...params });
				return ok(compact(r), r);
			},
		}),

		define({
			name: "delete_jobs",
			label: "Delete jobs",
			description: "Delete jobs. scope='pipeline' removes them from Applications and the extension queue (they stay searchable); 'index' removes them from the scraped job list; 'both' does both. Accepts job_ref values from search_jobs or pipeline job ids.",
			parameters: Type.Object({
				job_refs: Type.Array(Type.String(), { minItems: 1 }),
				scope: Type.Optional(Type.Union([Type.Literal("pipeline"), Type.Literal("index"), Type.Literal("both")])),
			}),
			execute: async ({ job_refs, scope }) => {
				const r = await api.post<Json>("/api/v1/jobindex/delete", { job_refs, scope: scope ?? "pipeline" });
				return ok(compact(r), r);
			},
		}),

		define({
			name: "update_job_status",
			label: "Update application stage",
			description: "Move a pipeline job to another stage: discovered, tailoring, approved, applied, interviewing, accepted, rejected, discarded.",
			parameters: Type.Object({
				job_id: Type.String(),
				status: Type.Union(["discovered", "tailoring", "approved", "applied", "interviewing", "accepted", "rejected", "discarded"].map((x) => Type.Literal(x))),
			}),
			execute: async ({ job_id, status }) => {
				const r = await api.put<Json>(`/api/v1/leads/${encodeURIComponent(job_id)}/status`, { status });
				return ok(compact({ job_id, status, ...r }), r);
			},
		}),

		define({
			name: "list_pipeline",
			label: "List applications",
			description: "List jobs in the user's pipeline (Applications), optionally filtered by stage, with their ids for update_job_status / delete_jobs / tailor_resume.",
			parameters: Type.Object({ status: Type.Optional(Type.String()), limit: Type.Optional(Type.Number()) }),
			execute: async ({ status, limit }) => {
				const q = new URLSearchParams({ limit: String(Math.min(200, Math.max(1, Math.floor(limit ?? 50)))) });
				if (status) q.set("status", status);
				const r = await api.get<Json | Json[]>(`/api/v1/leads?${q}`);
				const leads = (Array.isArray(r) ? r : (r.leads ?? r.items ?? [])) as Json[];
				const rows = leads.map((l) => ({ job_id: l.job_id, title: l.title, company: l.company, status: l.status, url: l.url, resume_ready: Boolean(l.resume_asset || l.asset) }));
				return ok(compact({ count: rows.length, jobs: rows }), rows);
			},
		}),

		define({
			name: "reset_jobs",
			label: "Reset job data",
			description: "IRREVERSIBLE. Empty the pipeline (Applications + extension queue), the scraped job index, or both. Only call after the user explicitly confirmed in this conversation; pass confirm='RESET'. Rebuilding the index needs a full scrape (hours).",
			parameters: Type.Object({
				scope: Type.Union([Type.Literal("pipeline"), Type.Literal("index"), Type.Literal("all")]),
				confirm: Type.String({ description: "Exactly RESET" }),
			}),
			execute: async ({ scope, confirm }) => {
				const r = await api.post<Json>("/api/v1/jobindex/reset", { scope, confirm });
				return ok(compact(r), r);
			},
		}),

		define({
			name: "edit_profile",
			label: "Edit profile",
			description: [
				"Change the user's profile precisely. Get ids from get_profile first.",
				"action 'update' replaces fields of one item (skill/experience/project by id); 'add' adds one item; 'delete' removes items (skills/experience/projects by id; education/certifications/achievements by their exact title text).",
				"section 'basics' updates name/summary/contact (no id needed).",
			].join(" "),
			parameters: Type.Object({
				action: Type.Union([Type.Literal("add"), Type.Literal("update"), Type.Literal("delete")]),
				section: Type.Union(["basics", "skill", "experience", "project", "education", "certification", "achievement"].map((x) => Type.Literal(x))),
				ids: Type.Optional(Type.Array(Type.String(), { description: "For delete/update: item ids, or titles for education/certification/achievement" })),
				fields: Type.Optional(Type.Object({
					name: Type.Optional(Type.String()), summary: Type.Optional(Type.String()),
					email: Type.Optional(Type.String()), phone: Type.Optional(Type.String()), city: Type.Optional(Type.String()),
					linkedin_url: Type.Optional(Type.String()), github_url: Type.Optional(Type.String()), website_url: Type.Optional(Type.String()),
					skill: Type.Optional(Type.String()), category: Type.Optional(Type.String()),
					role: Type.Optional(Type.String()), company: Type.Optional(Type.String()), period: Type.Optional(Type.String()),
					description: Type.Optional(Type.String({ description: "Bullet points, one per line starting with '- '" })),
					title: Type.Optional(Type.String()), stack: Type.Optional(Type.String()), repo: Type.Optional(Type.String()), impact: Type.Optional(Type.String()),
				})),
			}),
			execute: async ({ action, section, ids, fields }) => editProfile(api, action, section, ids ?? [], fields ?? {}, profileSummary),
		}),

		define({
			name: "schedule_check",
			label: "Schedule a check-in",
			description: [
				"Schedule yourself to wake up in THIS chat later and do something — e.g. check scrape progress, report new jobs from the last 24h, or check email.",
				"Use after start_scrape (scrapes take hours) or when the user asks for recurring updates. `instruction` is what you will be told when you wake.",
				"repeat_every_minutes makes it recurring until cancelled.",
			].join(" "),
			parameters: Type.Object({
				instruction: Type.String({ description: "What to do on wake-up, written to your future self" }),
				delay_minutes: Type.Number({ minimum: 1, maximum: 60 * 24 * 14 }),
				repeat_every_minutes: Type.Optional(Type.Number({ minimum: 15, maximum: 60 * 24 * 14 })),
			}),
			execute: async ({ instruction, delay_minutes, repeat_every_minutes }) => {
				const s = await ctx.scheduler.add({
					sessionId: ctx.sessionId(),
					instruction,
					delayMinutes: delay_minutes,
					repeatEveryMinutes: repeat_every_minutes,
				});
				return ok(compact({ scheduled: s }), s);
			},
		}),

		define({
			name: "list_schedules",
			label: "List check-ins",
			description: "Scheduled check-ins (all chats), with their next run time.",
			parameters: Type.Object({}),
			execute: async () => {
				const all = ctx.scheduler.list();
				return ok(compact({ schedules: all }), all);
			},
		}),

		define({
			name: "cancel_schedule",
			label: "Cancel check-in",
			description: "Cancel a scheduled check-in by id.",
			parameters: Type.Object({ schedule_id: Type.String() }),
			execute: async ({ schedule_id }) => {
				const removed = await ctx.scheduler.remove(schedule_id);
				return ok(compact({ cancelled: removed }), { removed });
			},
		}),
	];
}

async function editProfile(api: ApiClient, action: string, section: string, ids: string[], f: Json, summary: () => Promise<Json>) {
	const enc = encodeURIComponent;
	const done: Json[] = [];
	if (section === "basics") {
		const current = (await api.get<Json>("/api/v1/profile")) ?? {};
		if (f.name !== undefined || f.summary !== undefined) {
			await api.put("/api/v1/profile/candidate", { n: f.name ?? current.n ?? "", s: f.summary ?? current.s ?? "" });
		}
		const identity = Object.fromEntries(["email", "phone", "city", "linkedin_url", "github_url", "website_url"].filter((k) => f[k] !== undefined).map((k) => [k, f[k]]));
		if (Object.keys(identity).length) await api.put("/api/v1/profile/identity", identity);
		done.push({ updated: "basics" });
	} else if (action === "delete") {
		if (!ids.length) throw new Error("Give ids (or titles for education/certification/achievement) to delete");
		for (const id of ids) {
			await api.request("DELETE", `/api/v1/profile/${section}/${enc(id)}`);
			done.push({ deleted: id });
		}
	} else {
		const body: Json =
			section === "skill" ? { n: f.skill ?? f.title ?? "", cat: f.category ?? "general" }
			: section === "experience" ? { role: f.role ?? "", co: f.company ?? "", period: f.period ?? "", d: f.description ?? "" }
			: section === "project" ? { title: f.title ?? "", stack: f.stack ?? "", repo: f.repo ?? "", impact: f.impact ?? f.description ?? "" }
			: { title: f.title ?? "" };
		if (action === "update") {
			if (!["skill", "experience", "project"].includes(section)) throw new Error(`Items in ${section} cannot be edited in place: delete the old one and add the new one`);
			if (!ids[0]) throw new Error("Give the id of the item to update (see get_profile)");
			const current = ((await api.get<Json>("/api/v1/profile"))?.[section === "skill" ? "skills" : section === "experience" ? "exp" : "projects"] ?? []) as Json[];
			const existing = current.find((x) => x.id === ids[0]);
			if (!existing) throw new Error(`No ${section} with id ${ids[0]}`);
			const merged: Json = { ...existing };
			for (const [k, v] of Object.entries(body)) if (v !== "" && v !== undefined) merged[k] = v;
			delete merged.id;
			const allowed = section === "skill" ? ["n", "cat"] : section === "experience" ? ["role", "co", "period", "d"] : ["title", "stack", "repo", "impact"];
			const payload = Object.fromEntries(allowed.map((k) => [k, Array.isArray(merged[k]) ? merged[k].join(", ") : (merged[k] ?? "")]));
			await api.put(`/api/v1/profile/${section}/${enc(ids[0])}`, payload);
			done.push({ updated: ids[0] });
		} else {
			await api.post(`/api/v1/profile/${section}`, body);
			done.push({ added: section });
		}
	}
	const profile = await summary();
	return ok(compact({ done, profile }), { done, profile });
}

const DOC_PAGE = 24_000;

interface Extracted {
	text: string;
	pages?: number | null;
	method: string;
	images: { mime: string; data: string }[];
}

async function extractText(api: ApiClient, name: string, mime: string, bytes: Buffer): Promise<Extracted> {
	const form = new FormData();
	form.set("file", new Blob([new Uint8Array(bytes)], { type: mime || "application/octet-stream" }), name);
	return api.request<Extracted>("POST", "/api/v1/agent-docs/extract", undefined, { form, timeoutMs: 120_000 });
}

/** Text for the model, paged; scanned pages/images are returned as images for a vision model to read. */
function documentResult(doc: Extracted, offset: number, extra: Json) {
	const chunk = doc.text.slice(offset, offset + DOC_PAGE);
	const next = offset + DOC_PAGE < doc.text.length ? offset + DOC_PAGE : null;
	const header = {
		...extra, pages: doc.pages, method: doc.method, total_chars: doc.text.length,
		showing: doc.text.length ? `${offset}-${offset + chunk.length}` : "no text layer", next_offset: next,
		...(doc.images.length ? { note: "No text layer — the page images follow; read them directly." } : {}),
	};
	const content: Array<{ type: "text"; text: string } | { type: "image"; data: string; mimeType: string }> = [
		{ type: "text", text: `${compact(header)}\n\n${chunk}` },
		...doc.images.map((img) => ({ type: "image" as const, data: img.data, mimeType: img.mime })),
	];
	return { content, details: { ...header, preview: chunk.slice(0, 600) } };
}

export async function describeUpload(path: string): Promise<{ name: string; size: number }> {
	const s = await stat(path);
	return { name: basename(path), size: s.size };
}
