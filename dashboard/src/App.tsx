import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// ─── Types ────────────────────────────────────────────────────────────────────

type OpenPosition = {
  id: number; symbol: string; direction: string; qty: number;
  entry: number; stop: number; target: number;
  risk_dollars: number; reward_dollars: number; rr: number | null;
  current_price: number | null; price_ts: string | null;
  unrealized_pnl: number | null; opened_at: string; label: string | null;
};

type ActivityEvent = {
  ts: string; agent: string; type: string; summary: string; detail: string;
};

type ActiveStrategy = {
  symbol: string; strategy_name: string | null; engine: string | null;
  profit_factor: number | null; trades: number | null; total_pnl_dollars: number | null;
};

type Overview = {
  now_utc: string;
  session: { can_trade_now: boolean; reason: string };
  trading_stats: {
    headline: {
      pnl_today: number; trades_today: number; realized_pnl: number;
      win_rate: number; closed_trades: number;
    };
    by_symbol: { symbol: string; trade_count: number; realized_pnl: number }[];
  };
  research_stats: {
    headline: { active_strategy_count: number };
    active_strategies: ActiveStrategy[];
  };
  open_positions: OpenPosition[];
  week_stats?: { pnl: number; wins: number; losses: number; trades: number };
  youtube_stats?: { today: number; this_week: number };
  candidates?: { pending: number };
  activity_feed?: ActivityEvent[];
  regime?: { vol_regime: string; trend: string; leaders: string[] };
  daily_brief?: { date: string; snippet: string } | null;
  agents_last_run?: Record<string, string | null>;
};

type ChatEntry = { role: "user" | "assistant"; content: string };

// ─── Design tokens ────────────────────────────────────────────────────────────

const C = {
  bg:       "#07070f",
  surface:  "#0d0d1a",
  surface2: "#111120",
  border:   "#1a1a2e",
  text:     "#d1d5db",
  muted:    "#6b7280",
  green:    "#22c55e",
  red:      "#ef4444",
  gold:     "#f59e0b",
  blue:     "#6366f1",
  teal:     "#14b8a6",
  purple:   "#a78bfa",
  orange:   "#fb923c",
} as const;

const AGENT_COLORS: Record<string, string> = {
  researcher: C.blue, operator: C.gold, registrar: C.teal,
  scanner: C.green, coder: C.purple, trader: C.orange,
};

// ─── Utilities ────────────────────────────────────────────────────────────────

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

const pnlColor = (v: number | null | undefined) =>
  v == null ? C.muted : v > 0 ? C.green : v < 0 ? C.red : C.muted;

const pnlStr = (v: number | null | undefined) => {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : ""}$${Math.abs(v).toFixed(2)}`;
};

function ageMinutes(ts: string | null | undefined): number | null {
  if (!ts) return null;
  const d = new Date(ts);
  return isNaN(d.getTime()) ? null : (Date.now() - d.getTime()) / 60000;
}

function ageStr(ts: string | null | undefined): string {
  const m = ageMinutes(ts);
  if (m == null) return "never";
  if (m < 2) return "just now";
  if (m < 60) return `${Math.round(m)}m ago`;
  if (m < 1440) return `${(m / 60).toFixed(1)}h ago`;
  return `${Math.round(m / 1440)}d ago`;
}

const healthColor = (ts: string | null | undefined, warnM: number, critM: number) => {
  const m = ageMinutes(ts);
  if (m == null) return C.red;
  return m < warnM ? C.green : m < critM ? C.gold : C.red;
};

const fmtTime = (ts: string) => {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts.slice(11, 16) : d.toISOString().slice(11, 16) + "z";
};

// ─── Atoms ────────────────────────────────────────────────────────────────────

function Dot({ color, size = 7 }: { color: string; size?: number }) {
  return <span style={{ display: "inline-block", width: size, height: size, borderRadius: "50%", background: color, flexShrink: 0 }} />;
}

function Divider({ my = 10 }: { my?: number }) {
  return <div style={{ height: 1, background: C.border, margin: `${my}px 0` }} />;
}

function Label({ children, accent }: { children: ReactNode; accent?: string }) {
  return (
    <div style={{ fontSize: 9, fontWeight: 700, letterSpacing: "0.13em", textTransform: "uppercase", color: accent ?? C.muted, marginBottom: 8 }}>
      {children}
    </div>
  );
}

function Badge({ agent }: { agent: string }) {
  const color = AGENT_COLORS[agent.toLowerCase()] ?? C.muted;
  return (
    <span style={{ fontSize: 9, fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase", color, background: color + "22", border: `1px solid ${color}44`, borderRadius: 3, padding: "1px 5px" }}>
      {agent}
    </span>
  );
}

function Card({ title, children, accent, noPad }: { title?: string; children: ReactNode; accent?: string; noPad?: boolean }) {
  return (
    <div style={{ background: C.surface, border: `1px solid ${accent ?? C.border}`, borderRadius: 8, padding: noPad ? 0 : "11px 13px", marginBottom: 8, overflow: "hidden" }}>
      {title && <Label accent={accent ? accent : undefined}>{title}</Label>}
      {children}
    </div>
  );
}

function Kv({ k, v }: { k: string; v: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8, marginBottom: 3 }}>
      <span style={{ fontSize: 11, color: C.muted, flexShrink: 0 }}>{k}</span>
      <span style={{ fontSize: 11, color: C.text, fontFamily: "monospace", textAlign: "right" }}>{v}</span>
    </div>
  );
}

function ActionBtn({ label, onClick, disabled, loading, primary }: {
  label: string; onClick: () => void; disabled?: boolean; loading?: boolean; primary?: boolean;
}) {
  return (
    <button onClick={onClick} disabled={disabled} style={{
      background: primary ? C.blue : C.surface2,
      border: `1px solid ${primary ? C.blue + "aa" : C.border}`,
      color: disabled ? C.muted : primary ? "#fff" : C.text,
      borderRadius: 6, padding: "5px 11px", fontSize: 11,
      cursor: disabled ? "default" : "pointer", opacity: loading ? 0.65 : 1, whiteSpace: "nowrap",
    }}>
      {label}
    </button>
  );
}

// ─── Header ───────────────────────────────────────────────────────────────────

function Header({ ov }: { ov: Overview | null }) {
  const [clock, setClock] = useState(() => new Date().toISOString().slice(11, 19) + " UTC");
  useEffect(() => {
    const t = setInterval(() => setClock(new Date().toISOString().slice(11, 19) + " UTC"), 1000);
    return () => clearInterval(t);
  }, []);

  const runs = ov?.agents_last_run ?? {};
  const canTrade = ov?.session?.can_trade_now;

  const pills = [
    { label: "Session",   color: canTrade ? C.green : C.muted,                                    sub: canTrade ? "open" : "closed" },
    { label: "Scanner",   color: healthColor(runs["scanner_last_run"],     70,  150),              sub: ageStr(runs["scanner_last_run"]) },
    { label: "Research",  color: healthColor(runs["researcher_last_run"], 100,  200),              sub: ageStr(runs["researcher_last_run"]) },
    { label: "Operator",  color: healthColor(runs["operator_last_run"],   780, 1560),              sub: ageStr(runs["operator_last_run"]) },
    { label: "Registrar", color: healthColor(runs["registrar_last_run"],  600, 1200),              sub: ageStr(runs["registrar_last_run"]) },
  ];

  return (
    <div style={{ display: "flex", alignItems: "center", height: 42, padding: "0 16px", background: C.surface, borderBottom: `1px solid ${C.border}`, flexShrink: 0, gap: 20 }}>
      <span style={{ fontWeight: 800, fontSize: 12, letterSpacing: "0.1em", color: C.purple }}>◆ JUDAS CREW</span>
      <div style={{ display: "flex", gap: 18, flex: 1 }}>
        {pills.map((p) => (
          <div key={p.label} style={{ display: "flex", alignItems: "center", gap: 5 }}>
            <Dot color={p.color} />
            <span style={{ fontSize: 10, color: C.muted }}>{p.label}</span>
            <span style={{ fontSize: 10, color: p.color === C.red ? C.red : C.muted, fontFamily: "monospace" }}>{p.sub}</span>
          </div>
        ))}
      </div>
      <span style={{ fontSize: 11, fontFamily: "monospace", color: C.muted }}>{clock}</span>
    </div>
  );
}

// ─── Left panel ───────────────────────────────────────────────────────────────

function LeftPanel({ ov }: { ov: Overview | null }) {
  const hl      = ov?.trading_stats?.headline;
  const wk      = ov?.week_stats;
  const yt      = ov?.youtube_stats;
  const res     = ov?.research_stats?.headline;
  const cands   = ov?.candidates;
  const regime  = ov?.regime;
  const brief   = ov?.daily_brief;

  const totalClosed = hl?.closed_trades ?? 0;
  const totalWins   = Math.round((hl?.win_rate ?? 0) * totalClosed);
  const totalLosses = totalClosed - totalWins;

  const coveredSymbols = new Set((ov?.research_stats?.active_strategies ?? []).map((s) => s.symbol)).size;

  return (
    <div style={{ width: 256, background: C.surface, borderRight: `1px solid ${C.border}`, overflowY: "auto", padding: 10, flexShrink: 0 }}>

      <Card>
        <Label>Today</Label>
        <div style={{ fontFamily: "monospace", fontSize: 26, fontWeight: 800, color: pnlColor(hl?.pnl_today), lineHeight: 1.05, marginBottom: 4 }}>
          {pnlStr(hl?.pnl_today ?? null)}
        </div>
        <div style={{ fontSize: 11, color: C.muted }}>{hl?.trades_today ?? 0} trades</div>
      </Card>

      <Card>
        <Label>This Week</Label>
        <div style={{ fontFamily: "monospace", fontSize: 22, fontWeight: 800, color: pnlColor(wk?.pnl), lineHeight: 1.05, marginBottom: 4 }}>
          {pnlStr(wk?.pnl ?? null)}
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <span style={{ fontSize: 12, color: C.green, fontFamily: "monospace" }}>{wk?.wins ?? 0}W</span>
          <span style={{ fontSize: 12, color: C.red,   fontFamily: "monospace" }}>{wk?.losses ?? 0}L</span>
          {(wk?.wins ?? 0) + (wk?.losses ?? 0) > 0 && (
            <span style={{ fontSize: 10, color: C.muted }}>
              {Math.round(((wk?.wins ?? 0) / ((wk?.wins ?? 0) + (wk?.losses ?? 0))) * 100)}% WR
            </span>
          )}
        </div>
      </Card>

      <Card>
        <Label>All Time</Label>
        <div style={{ fontFamily: "monospace", fontSize: 18, fontWeight: 700, color: pnlColor(hl?.realized_pnl), marginBottom: 4 }}>
          {pnlStr(hl?.realized_pnl ?? null)}
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <span style={{ fontSize: 11, color: C.green, fontFamily: "monospace" }}>{totalWins}W</span>
          <span style={{ fontSize: 11, color: C.red,   fontFamily: "monospace" }}>{totalLosses}L</span>
          {totalClosed > 0 && (
            <span style={{ fontSize: 10, color: C.muted }}>{Math.round((hl?.win_rate ?? 0) * 100)}% WR</span>
          )}
        </div>
      </Card>

      <Divider />

      <Card title="Research Pipeline">
        <Kv k="📺 YouTube / wk" v={`${yt?.this_week ?? 0}  (${yt?.today ?? 0} today)`} />
        <Kv k="📋 Candidates"   v={`${cands?.pending ?? 0} pending`} />
        <Kv k="⚡ Active strats" v={`${res?.active_strategy_count ?? 0} / ${coveredSymbols} syms`} />
      </Card>

      {regime && (
        <Card title="Regime">
          <Kv k="Trend"   v={regime.trend} />
          <Kv k="Vol"     v={regime.vol_regime} />
          {regime.leaders.length > 0 && <Kv k="Leaders" v={regime.leaders.join(", ")} />}
        </Card>
      )}

      {brief && (
        <Card title={`Brief · ${brief.date}`}>
          <div style={{ fontSize: 10, color: C.muted, lineHeight: 1.55, whiteSpace: "pre-wrap" }}>{brief.snippet}</div>
        </Card>
      )}
    </div>
  );
}

// ─── Center panel ─────────────────────────────────────────────────────────────

function SectionTitle({ children }: { children: ReactNode }) {
  return <div style={{ fontSize: 9, fontWeight: 700, letterSpacing: "0.13em", textTransform: "uppercase", color: C.muted, marginBottom: 6, paddingLeft: 2 }}>{children}</div>;
}

function EmptySlate({ children }: { children: ReactNode }) {
  return <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, padding: "14px 12px", color: C.muted, fontSize: 11, textAlign: "center" }}>{children}</div>;
}

function MiniStat({ label, val, color }: { label: string; val: string; color?: string }) {
  return (
    <div>
      <div style={{ fontSize: 9, color: C.muted, textTransform: "uppercase", letterSpacing: "0.08em" }}>{label}</div>
      <div style={{ fontSize: 12, fontFamily: "monospace", color: color ?? C.text }}>{val}</div>
    </div>
  );
}

function PositionRow({ pos }: { pos: OpenPosition }) {
  const upnl = pos.unrealized_pnl;
  const borderAccent = upnl != null && upnl > 0 ? C.green + "50" : upnl != null && upnl < 0 ? C.red + "50" : C.border;
  return (
    <div style={{ background: C.surface, border: `1px solid ${borderAccent}`, borderRadius: 8, padding: "10px 14px", marginBottom: 6, display: "grid", gridTemplateColumns: "70px 1fr auto", alignItems: "center", gap: 14 }}>
      <div>
        <div style={{ fontFamily: "monospace", fontWeight: 800, fontSize: 14, color: C.text }}>{pos.symbol}</div>
        <div style={{ fontSize: 10, color: pos.direction === "long" ? C.green : C.red, fontWeight: 700, textTransform: "uppercase", marginTop: 1 }}>{pos.direction}</div>
      </div>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <MiniStat label="Entry"  val={pos.entry.toFixed(2)} />
        {pos.current_price != null && <MiniStat label="Now" val={pos.current_price.toFixed(2)} />}
        <MiniStat label="Stop"   val={pos.stop.toFixed(2)}   color={C.red} />
        <MiniStat label="Target" val={pos.target.toFixed(2)} color={C.green} />
        {pos.rr != null && <MiniStat label="R/R" val={`1:${pos.rr.toFixed(1)}`} />}
      </div>
      <div style={{ textAlign: "right" }}>
        <div style={{ fontFamily: "monospace", fontWeight: 800, fontSize: 18, color: pnlColor(upnl) }}>{pnlStr(upnl)}</div>
        <div style={{ fontSize: 9, color: C.muted, marginTop: 1 }}>unrealized</div>
      </div>
    </div>
  );
}

function CenterPanel({ ov }: { ov: Overview | null }) {
  const positions = ov?.open_positions ?? [];
  const tradeFeed = (ov?.activity_feed ?? []).filter((e) => e.type === "trade").slice(0, 10);
  const strats    = [...(ov?.research_stats?.active_strategies ?? [])]
    .filter((s) => s.profit_factor != null)
    .sort((a, b) => (b.profit_factor ?? 0) - (a.profit_factor ?? 0))
    .slice(0, 10);

  return (
    <div style={{ flex: 1, overflowY: "auto", padding: 10 }}>
      {positions.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, paddingLeft: 2 }}>
            <span style={{ fontSize: 9, fontWeight: 700, letterSpacing: "0.13em", textTransform: "uppercase", color: C.gold }}>Open Positions</span>
            <span style={{ fontSize: 9, background: C.gold + "28", color: C.gold, borderRadius: 4, padding: "0 5px", fontFamily: "monospace" }}>{positions.length}</span>
          </div>
          {positions.map((pos) => <PositionRow key={pos.id} pos={pos} />)}
        </div>
      )}

      <div style={{ marginBottom: 14 }}>
        <SectionTitle>Recent Trades</SectionTitle>
        {tradeFeed.length === 0
          ? <EmptySlate>No trades yet</EmptySlate>
          : (
            <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, overflow: "hidden" }}>
              {tradeFeed.map((t, i) => (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: 12, padding: "8px 12px", borderBottom: i < tradeFeed.length - 1 ? `1px solid ${C.border}30` : "none" }}>
                  <span style={{ fontSize: 10, color: C.muted, fontFamily: "monospace", flexShrink: 0 }}>{fmtTime(t.ts)}</span>
                  <span style={{ fontSize: 12, color: C.text, flex: 1 }}>{t.summary}</span>
                  {t.detail && <span style={{ fontSize: 10, color: C.muted }}>{t.detail}</span>}
                </div>
              ))}
            </div>
          )
        }
      </div>

      <div>
        <SectionTitle>Strategy Leaderboard</SectionTitle>
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, overflow: "hidden" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ background: C.surface2 }}>
                {["#", "Symbol", "Strategy", "PF", "Trades", "P&L"].map((h) => (
                  <th key={h} style={{ padding: "7px 11px", textAlign: h === "#" ? "center" : "left", color: C.muted, fontSize: 9, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", borderBottom: `1px solid ${C.border}` }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {strats.length === 0
                ? <tr><td colSpan={6} style={{ padding: "18px 12px", color: C.muted, textAlign: "center", fontSize: 11 }}>No active strategies</td></tr>
                : strats.map((s, i) => (
                  <tr key={i} style={{ borderBottom: `1px solid ${C.border}20` }}>
                    <td style={{ padding: "7px 11px", color: C.muted, textAlign: "center", fontFamily: "monospace" }}>{i + 1}</td>
                    <td style={{ padding: "7px 11px", fontFamily: "monospace", fontWeight: 700, color: C.text }}>{s.symbol}</td>
                    <td style={{ padding: "7px 11px", color: C.muted, fontSize: 11 }}>{s.strategy_name ?? s.engine ?? "—"}</td>
                    <td style={{ padding: "7px 11px", fontFamily: "monospace", color: C.blue, fontWeight: 700 }}>{s.profit_factor?.toFixed(2) ?? "—"}</td>
                    <td style={{ padding: "7px 11px", fontFamily: "monospace", color: C.muted }}>{s.trades ?? "—"}</td>
                    <td style={{ padding: "7px 11px", fontFamily: "monospace", color: pnlColor(s.total_pnl_dollars), fontWeight: 600 }}>{pnlStr(s.total_pnl_dollars)}</td>
                  </tr>
                ))
              }
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ─── Right panel — activity feed ──────────────────────────────────────────────

function ActivityItem({ ev }: { ev: ActivityEvent }) {
  return (
    <div style={{ background: C.surface2, border: `1px solid ${C.border}`, borderRadius: 6, padding: "8px 9px", marginBottom: 5 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 3 }}>
        <Badge agent={ev.agent} />
        <span style={{ fontSize: 9, color: C.muted, fontFamily: "monospace", marginLeft: "auto" }}>{fmtTime(ev.ts)}</span>
      </div>
      <div style={{ fontSize: 11, color: C.text, lineHeight: 1.45 }}>{ev.summary}</div>
      {ev.detail && <div style={{ fontSize: 10, color: C.muted, marginTop: 2, lineHeight: 1.3 }}>{ev.detail}</div>}
    </div>
  );
}

function RightPanel({ ov }: { ov: Overview | null }) {
  const feed = ov?.activity_feed ?? [];
  return (
    <div style={{ width: 296, background: C.surface, borderLeft: `1px solid ${C.border}`, overflowY: "auto", padding: 10, flexShrink: 0 }}>
      <Label>Activity Feed</Label>
      {feed.length === 0 && <div style={{ color: C.muted, fontSize: 11, padding: "10px 2px" }}>No activity yet</div>}
      {feed.map((ev, i) => <ActivityItem key={i} ev={ev} />)}
    </div>
  );
}

// ─── Chat bar ─────────────────────────────────────────────────────────────────

function ChatBar() {
  const [chat, setChat]         = useState<ChatEntry[]>([]);
  const [msg, setMsg]           = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [busyPath, setBusyPath] = useState<string | null>(null);
  const [ytQuery, setYtQuery]   = useState("");
  const [expanded, setExpanded] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [chat]);

  async function runAction(path: string, body?: object) {
    setBusyPath(path);
    try {
      const data = await apiFetch<{ ok: boolean; output: string }>(path, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      setChat((c) => [...c, { role: "assistant", content: data.output ?? (data.ok ? "Done." : "Failed.") }]);
      setExpanded(true);
    } catch (e) {
      setChat((c) => [...c, { role: "assistant", content: `Error: ${e}` }]);
      setExpanded(true);
    } finally { setBusyPath(null); }
  }

  async function send() {
    const m = msg.trim();
    if (!m || chatBusy) return;
    setMsg("");
    setChat((c) => [...c, { role: "user", content: m }]);
    setChatBusy(true); setExpanded(true);
    try {
      const data = await apiFetch<{ response: string }>("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: m }),
      });
      setChat((c) => [...c, { role: "assistant", content: data.response }]);
    } catch (e) {
      setChat((c) => [...c, { role: "assistant", content: `Error: ${e}` }]);
    } finally { setChatBusy(false); }
  }

  const visible = chat.slice(-10);

  return (
    <div style={{ background: C.surface, borderTop: `1px solid ${C.border}`, flexShrink: 0 }}>
      {expanded && visible.length > 0 && (
        <div style={{ maxHeight: 220, overflowY: "auto", padding: "8px 14px", borderBottom: `1px solid ${C.border}` }}>
          {visible.map((e, i) => (
            <div key={i} style={{ display: "flex", flexDirection: "column", alignItems: e.role === "user" ? "flex-end" : "flex-start", marginBottom: 6 }}>
              <div style={{ maxWidth: "72%", background: e.role === "user" ? C.blue + "25" : C.surface2, border: `1px solid ${e.role === "user" ? C.blue + "44" : C.border}`, borderRadius: 8, padding: "6px 10px", fontSize: 12, color: C.text, lineHeight: 1.5 }}>
                {e.role === "assistant"
                  ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{e.content}</ReactMarkdown>
                  : e.content}
              </div>
            </div>
          ))}
          {chatBusy && <div style={{ fontSize: 11, color: C.muted, padding: "2px 0" }}>thinking…</div>}
          <div ref={bottomRef} />
        </div>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 7, padding: "7px 14px" }}>
        {chat.length > 0 && (
          <button onClick={() => setExpanded((x) => !x)} style={{ background: "none", border: "none", color: C.muted, cursor: "pointer", fontSize: 14, lineHeight: 1, padding: 0 }}>
            {expanded ? "▼" : "▲"}
          </button>
        )}
        <input type="text" value={msg} onChange={(e) => setMsg(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Ask anything about the portfolio…" disabled={chatBusy}
          style={{ flex: 1, background: C.surface2, border: `1px solid ${C.border}`, borderRadius: 6, padding: "7px 11px", fontSize: 12, color: C.text, outline: "none" }} />
        <ActionBtn label={chatBusy ? "…" : "Send"} onClick={send} disabled={chatBusy || !msg.trim()} primary />
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "0 14px 8px", flexWrap: "wrap" }}>
        <ActionBtn label={busyPath === "/api/run/researcher" ? "Running…" : "Run Researcher"} onClick={() => runAction("/api/run/researcher")} disabled={!!busyPath} loading={busyPath === "/api/run/researcher"} />
        <input type="text" value={ytQuery} onChange={(e) => setYtQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ytQuery.trim() && runAction("/api/run/youtube", { query: ytQuery })}
          placeholder="YouTube concept…"
          style={{ background: C.surface2, border: `1px solid ${C.border}`, borderRadius: 6, padding: "5px 10px", fontSize: 11, color: C.text, width: 148, outline: "none" }} />
        <ActionBtn label={busyPath === "/api/run/youtube" ? "Searching…" : "YouTube ▶"} onClick={() => runAction("/api/run/youtube", { query: ytQuery })} disabled={!!busyPath || !ytQuery.trim()} loading={busyPath === "/api/run/youtube"} />
        <ActionBtn label={busyPath === "/api/run/sweep" ? "Sweeping…" : "Sweep All Symbols"} onClick={() => runAction("/api/run/sweep")} disabled={!!busyPath} loading={busyPath === "/api/run/sweep"} />
        <ActionBtn label={busyPath === "/api/run/doctor" ? "Checking…" : "Doctor"} onClick={() => runAction("/api/run/doctor", { symbol: "MGC" })} disabled={!!busyPath} loading={busyPath === "/api/run/doctor"} />
      </div>
    </div>
  );
}

// ─── Root ─────────────────────────────────────────────────────────────────────

export default function App() {
  const [ov, setOv] = useState<Overview | null>(null);

  useEffect(() => {
    const load = () => apiFetch<Overview>("/api/overview").then(setOv).catch(() => {});
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, []);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", background: C.bg, color: C.text, fontFamily: "system-ui,-apple-system,sans-serif", overflow: "hidden" }}>
      <Header ov={ov} />
      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
        <LeftPanel ov={ov} />
        <CenterPanel ov={ov} />
        <RightPanel ov={ov} />
      </div>
      <ChatBar />
    </div>
  );
}
