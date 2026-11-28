// Small stroke icons drawn for Agent-Hire (24×24 grid, currentColor).
const PATHS: Record<string, string> = {
  sparkle: "M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8",
  mail: "M3.5 6.5h17v11h-17zM4 7l8 6 8-6",
  globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3.5 12h17M12 3c2.5 2.6 3.6 5.6 3.6 9s-1.1 6.4-3.6 9c-2.5-2.6-3.6-5.6-3.6-9S9.5 5.6 12 3z",
  gear: "M12 9.2a2.8 2.8 0 1 0 0 5.6 2.8 2.8 0 0 0 0-5.6zM19.4 13.5l1.6 1.2-1.8 3.1-1.9-.7a7.3 7.3 0 0 1-1.8 1l-.3 2h-3.6l-.3-2a7.3 7.3 0 0 1-1.8-1l-1.9.7-1.8-3.1 1.6-1.2a7 7 0 0 1 0-2.1L3.8 10.3l1.8-3.1 1.9.7a7.3 7.3 0 0 1 1.8-1l.3-2h3.6l.3 2a7.3 7.3 0 0 1 1.8 1l1.9-.7 1.8 3.1-1.6 1.1a7 7 0 0 1 0 2.1z",
  check: "M5 12.5l4.2 4.2L19 7",
  sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4",
  moon: "M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10z",
  activity: "M3 12h4l2.5-6 5 12 2.5-6h4",
  trash: "M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13M10 11v5M14 11v5",
  close: "M6 6l12 12M18 6L6 18",
  chevron: "M8 10l4 4 4-4",
  external: "M14 4.5h5.5V10M19.5 4.5l-8 8M17 14v5.5H4.5V7H10",
};

export type IconName = keyof typeof PATHS;

export default function Icon({ name, size = 16, className }: { name: IconName | string; size?: number; className?: string }) {
  return (
    <svg className={`icon ${className ?? ""}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
      <path d={PATHS[name] ?? ""} />
    </svg>
  );
}
