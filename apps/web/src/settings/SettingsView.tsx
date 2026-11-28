// Settings: AI model · Email updates · Job agent · General.
import type { ApiFetch, SettingsTab } from "../types";
import Icon from "../ui/Icon";
import { AiModelSettings } from "./AiModelSettings";
import { EmailOutcomeSettings } from "./EmailOutcomeSettings";
import { GeneralSettings } from "./GeneralSettings";
import { AgentSettings } from "../agent/AgentSettings";

const TABS: { id: SettingsTab; label: string; icon: string }[] = [
  { id: "ai", label: "AI model", icon: "sparkle" },
  { id: "email", label: "Email updates", icon: "mail" },
  { id: "agent", label: "Job agent", icon: "globe" },
  { id: "general", label: "General", icon: "gear" },
];

export default function SettingsView({ api, tab, onTab }: { api: ApiFetch; tab: SettingsTab; onTab: (tab: SettingsTab) => void }) {
  return (
    <div className="settings-page">
      <div className="settings-inner">
        <nav className="seg-tabs" role="tablist" aria-label="Settings sections">
          {TABS.map((item) => (
            <button key={item.id} role="tab" aria-selected={tab === item.id} className={tab === item.id ? "active" : ""} onClick={() => onTab(item.id)}>
              <Icon name={item.icon} size={14} />
              {item.label}
            </button>
          ))}
        </nav>
        <div className="settings-card" role="tabpanel">
          {tab === "ai" && <AiModelSettings api={api} />}
          {tab === "email" && <div className="settings-box"><EmailOutcomeSettings api={api} /></div>}
          {tab === "agent" && <AgentSettings api={api} />}
          {tab === "general" && <GeneralSettings api={api} />}
        </div>
      </div>
    </div>
  );
}
