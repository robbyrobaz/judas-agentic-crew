import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { Activity, Bot, Clock3, FlaskConical, Play, SendHorizonal, ShieldAlert, Signal, TrendingUp } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

type Overview = {
  now_utc: string;
  now_et: string;
  now_phx: string;
  session: Record<string, unknown>;
  services: Record<string, string>;
  research_runtime: Record<string, unknown>;
  counts: Record<string, number>;
  latest_signal: Record<string, unknown> | null;
  latest_trade: Record<string, unknown> | null;
  latest_experiment: Record<string, unknown> | null;
};

type ChatEntry = { role: "user" | "assistant"; content: string };

async function getJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function App() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [signals, setSignals] = useState<Record<string, unknown>[]>([]);
  const [trades, setTrades] = useState<Record<string, unknown>[]>([]);
  const [experiments, setExperiments] = useState<Record<string, unknown>[]>([]);
  const [chat, setChat] = useState<ChatEntry[]>([]);
  const [message, setMessage] = useState("");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [chatBusy, setChatBusy] = useState(false);

  const refresh = async () => {
    const [overviewData, signalsData, tradesData, experimentsData] = await Promise.all([
      getJson<Overview>("/api/overview"),
      getJson<{ signals: Record<string, unknown>[] }>("/api/signals"),
      getJson<{ trades: Record<string, unknown>[] }>("/api/trades"),
      getJson<{ experiments: Record<string, unknown>[] }>("/api/experiments"),
    ]);
    setOverview(overviewData);
    setSignals(signalsData.signals);
    setTrades(tradesData.trades);
    setExperiments(experimentsData.experiments);
  };

  useEffect(() => {
    refresh().catch(console.error);
    const interval = window.setInterval(() => {
      refresh().catch(console.error);
    }, 30000);
    return () => window.clearInterval(interval);
  }, []);

  const sendMessage = async () => {
    const trimmed = message.trim();
    if (!trimmed || chatBusy) return;
    setChat((current) => [...current, { role: "user", content: trimmed }]);
    setMessage("");
    setChatBusy(true);
    try {
      const data = await getJson<{ response: string; history: ChatEntry[] }>("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: trimmed }),
      });
      setChat(data.history);
      await refresh();
    } finally {
      setChatBusy(false);
    }
  };

  const runAction = async (path: "/api/run/doctor" | "/api/run/research") => {
    setBusyAction(path);
    try {
      const data = await getJson<{ ok: boolean; output: string }>(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: "MGC" }),
      });
      setChat((current) => [
        ...current,
        {
          role: "assistant",
          content: `${path === "/api/run/doctor" ? "Doctor" : "Research"} run ${data.ok ? "completed" : "failed"}.\n\n${data.output}`,
        },
      ]);
      await refresh();
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <div className="min-h-screen bg-background text-foreground bg-haze font-body">
      <div className="mx-auto max-w-[1500px] px-6 pb-8 pt-8">
        <header className="grid gap-6 lg:grid-cols-[1.35fr_0.65fr]">
          <section className="rounded-[32px] border border-line/70 bg-panel/90 p-8 shadow-panel backdrop-blur-md">
            <div className="flex items-start justify-between gap-6">
              <div>
                <p className="text-xs uppercase tracking-[0.32em] text-accent/70">Judas Operator Manager</p>
                <h1 className="mt-3 font-display text-5xl leading-none">One manager over trading and research</h1>
                <p className="mt-4 max-w-2xl text-sm leading-6 text-foreground/75">
                  This manager watches the live paper trading path, the research lab cadence, timer health, and recent experiment output. Backend timestamps stay in UTC, but operator conversation is framed in Phoenix time.
                </p>
              </div>
              <div className="rounded-3xl border border-line/70 bg-white/70 px-4 py-3 text-right">
                <p className="text-xs uppercase tracking-[0.28em] text-foreground/45">Operator Time</p>
                <p className="mt-2 text-lg font-semibold">{overview?.now_phx ?? "loading..."}</p>
                <p className="text-xs text-foreground/55">Tailnet: omen-claw.tail76e7df.ts.net</p>
              </div>
            </div>
            <div className="mt-8 grid gap-4 md:grid-cols-4">
              <MetricCard icon={Clock3} label="Session" value={String(overview?.session?.active_session ?? "closed")} detail={String(overview?.session?.reason ?? "loading")} />
              <MetricCard icon={Signal} label="Signals" value={String(overview?.counts?.signals ?? 0)} detail={String(overview?.latest_signal?.direction ?? "none")} />
              <MetricCard icon={TrendingUp} label="Trades" value={String(overview?.counts?.trades ?? 0)} detail={String(overview?.counts?.open_trades ?? 0) + " open"} />
              <MetricCard icon={FlaskConical} label="Experiments" value={String(overview?.counts?.experiments ?? 0)} detail={String(overview?.latest_experiment?.experiment_type ?? "none")} />
            </div>
          </section>

          <section className="rounded-[32px] border border-line/70 bg-[#173c34] p-8 text-white shadow-panel">
            <p className="text-xs uppercase tracking-[0.32em] text-white/60">Operator Actions</p>
            <div className="mt-5 grid gap-3">
              <ActionButton title="Run Doctor" subtitle="Connectivity, LLM, IBKR, session checks" icon={ShieldAlert} loading={busyAction === "/api/run/doctor"} onClick={() => runAction("/api/run/doctor")} />
              <ActionButton title="Run Research" subtitle="Kick off the research scheduler manually" icon={Play} loading={busyAction === "/api/run/research"} onClick={() => runAction("/api/run/research")} />
            </div>
            <div className="mt-8 rounded-3xl border border-white/10 bg-white/5 p-5">
              <p className="text-xs uppercase tracking-[0.28em] text-white/55">Research Runtime</p>
              <p className="mt-2 text-xl font-semibold capitalize">{String(overview?.research_runtime?.state ?? "unknown")}</p>
              <p className="mt-2 text-sm leading-6 text-white/70">
                {overview?.research_runtime?.started_at_utc
                  ? `Started ${String(overview.research_runtime.started_at_utc)} UTC`
                  : "No recent runtime state recorded."}
              </p>
            </div>
          </section>
        </header>

        <main className="mt-8 grid gap-6 lg:grid-cols-[0.95fr_1.05fr]">
          <section className="grid gap-6">
            <Panel title="Service State" icon={Bot}>
              <div className="grid gap-3 md:grid-cols-2">
                {Object.entries(overview?.services ?? {}).map(([unit, status]) => (
                  <div key={unit} className="rounded-3xl border border-line/70 bg-white/60 p-4">
                    <p className="text-xs uppercase tracking-[0.24em] text-foreground/45">{unit}</p>
                    <p className="mt-2 text-sm leading-6 text-foreground/75">{status}</p>
                  </div>
                ))}
              </div>
            </Panel>
            <Panel title="Recent Signals" icon={Signal}>
              <MiniTable rows={signals} fields={["ts_utc", "symbol", "direction", "quality_score", "risk_decision"]} />
            </Panel>
            <Panel title="Recent Trades" icon={Activity}>
              <MiniTable rows={trades} fields={["opened_at", "symbol", "direction", "qty", "status", "pnl_dollars"]} />
            </Panel>
            <Panel title="Recent Experiments" icon={FlaskConical}>
              <MiniTable rows={experiments} fields={["ts_utc", "experiment_type", "name", "status", "summary"]} />
            </Panel>
          </section>

          <section className="sticky top-6 h-[calc(100vh-3rem)]">
            <Panel title="Operator Manager Conversation" icon={Bot}>
              <div className="flex h-[calc(100vh-10rem)] flex-col rounded-[28px] border border-line/70 bg-white/60 p-5">
                <div className="mb-4 flex items-center gap-3">
                  <div className="flex h-12 w-12 items-center justify-center rounded-full bg-accent text-2xl text-white">🧭</div>
                  <div>
                    <p className="text-sm uppercase tracking-[0.28em] text-foreground/45">Manager</p>
                    <p className="text-xl font-semibold">Unified operator chat</p>
                  </div>
                </div>
                <div className="flex-1 space-y-3 overflow-y-auto pr-2">
                  {chat.length === 0 ? (
                    <div className="rounded-3xl border border-dashed border-line bg-background/70 p-4 text-sm text-foreground/65">
                      Ask about trading cadence, research progress, timer failures, recent experiments, or tell the manager to run doctor or research.
                    </div>
                  ) : (
                    chat.map((entry, index) => (
                      <div
                        key={`${entry.role}-${index}`}
                        className={cn(
                          "rounded-3xl px-4 py-3 text-sm leading-6",
                          entry.role === "assistant" ? "bg-accent text-white" : "bg-sand/25 text-foreground",
                        )}
                      >
                        <p className="mb-1 text-[11px] uppercase tracking-[0.24em] opacity-70">{entry.role}</p>
                        <div className="markdown-body">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.content}</ReactMarkdown>
                        </div>
                      </div>
                    ))
                  )}
                </div>
                <div className="mt-4 border-t border-line/70 pt-4">
                  <p className="mb-2 text-xs uppercase tracking-[0.28em] text-foreground/45">Operator Manager</p>
                  <div className="flex items-end gap-3">
                    <textarea
                      value={message}
                      onChange={(event) => setMessage(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && !event.shiftKey) {
                          event.preventDefault();
                          sendMessage().catch(console.error);
                        }
                      }}
                      placeholder="Ask the manager what is running, what failed, whether research is healthy, or tell it to run doctor or research..."
                      className="min-h-[92px] flex-1 resize-none rounded-3xl border border-line/70 bg-background/70 px-4 py-3 text-sm leading-6 outline-none placeholder:text-foreground/45"
                    />
                    <button
                      onClick={() => sendMessage().catch(console.error)}
                      disabled={chatBusy || !message.trim()}
                      className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-accent text-white transition hover:bg-[#215f4c] disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <SendHorizonal size={18} />
                    </button>
                  </div>
                </div>
              </div>
            </Panel>
          </section>

        </main>
      </div>
    </div>
  );
}

function MetricCard({
  icon: Icon,
  label,
  value,
  detail,
}: {
  icon: typeof Clock3;
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <div className="rounded-3xl border border-line/70 bg-white/70 p-4">
      <div className="flex items-center gap-3">
        <div className="rounded-2xl bg-accent p-2 text-white"><Icon size={18} /></div>
        <div>
          <p className="text-xs uppercase tracking-[0.24em] text-foreground/45">{label}</p>
          <p className="text-xl font-semibold capitalize">{value}</p>
        </div>
      </div>
      <p className="mt-3 text-xs text-foreground/60">{detail}</p>
    </div>
  );
}

function Panel({
  title,
  icon: Icon,
  children,
}: {
  title: string;
  icon: typeof Clock3;
  children: ReactNode;
}) {
  return (
    <section className="rounded-[32px] border border-line/70 bg-panel/90 p-6 shadow-panel">
      <div className="mb-4 flex items-center gap-3">
        <div className="rounded-2xl bg-berry p-2 text-white"><Icon size={18} /></div>
        <h2 className="font-display text-2xl">{title}</h2>
      </div>
      {children}
    </section>
  );
}

function MiniTable({
  rows,
  fields,
}: {
  rows: Record<string, unknown>[];
  fields: string[];
}) {
  return (
    <div className="overflow-x-auto rounded-[24px] border border-line/70 bg-white/60">
      <table className="min-w-full text-left text-sm">
        <thead className="border-b border-line/70 bg-white/70">
          <tr>
            {fields.map((field) => (
              <th key={field} className="px-4 py-3 text-[11px] uppercase tracking-[0.24em] text-foreground/50">
                {field.split("_").join(" ")}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={fields.length} className="px-4 py-6 text-center text-foreground/55">
                No data yet
              </td>
            </tr>
          ) : (
            rows.map((row, index) => (
              <tr key={index} className="border-b border-line/40 last:border-b-0">
                {fields.map((field) => (
                  <td key={field} className="px-4 py-3 align-top text-foreground/80">
                    {String(row[field] ?? "")}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function ActionButton({
  title,
  subtitle,
  icon: Icon,
  loading,
  onClick,
}: {
  title: string;
  subtitle: string;
  icon: typeof Play;
  loading: boolean;
  onClick: () => void;
}) {
  return (
    <button
      className="rounded-3xl border border-white/10 bg-white/5 px-5 py-4 text-left transition hover:bg-white/10"
      onClick={onClick}
      disabled={loading}
    >
      <div className="flex items-center gap-3">
        <div className="rounded-2xl bg-white/10 p-2"><Icon size={18} /></div>
        <div>
          <p className="font-semibold">{loading ? "Running..." : title}</p>
          <p className="text-sm text-white/65">{subtitle}</p>
        </div>
      </div>
    </button>
  );
}

export default App;
