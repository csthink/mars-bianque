import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useParams } from "react-router";
import { NotFoundError, useCalls, useInvestigation } from "../api/client";
import { useInvestigationEvents } from "../api/events";
import type { StepView } from "../api/types";
import { AlertCard } from "../components/AlertCard";
import { CallTable } from "../components/CallTable";
import { CallTimeline } from "../components/CallTimeline";
import { CitableIds } from "../components/citableContext";
import { EvidenceList } from "../components/EvidenceList";
import { FlowGraph } from "../components/FlowGraph";
import { FollowupPanel } from "../components/FollowupPanel";
import { HeaderChip, Layout } from "../components/Layout";
import { PlanPanel, type StepFocus } from "../components/PlanPanel";
import { ProgressFeed } from "../components/ProgressFeed";
import { ReportPanel } from "../components/ReportPanel";
import { SuggestionList } from "../components/SuggestionList";
import { Muted, Section } from "../components/ui";
import { stepIndex } from "../lib/events";
import { isOpenStatus, profileLabel } from "../lib/format";

/**
 * The evidence or suggestion named by the URL hash (``#ev_…``, ``#sg_…``): scrolled into
 * view and highlighted.
 */
function useCitationTarget(ready: boolean): string | null {
  const { hash } = useLocation();
  const target = ready && /^#(ev|sg)_/.test(hash) ? decodeURIComponent(hash.slice(1)) : null;
  useEffect(() => {
    if (target !== null) document.getElementById(target)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [target, hash]);
  return target;
}

export function InvestigationDetailPage() {
  const { id = "" } = useParams();
  const detail = useInvestigation(id);
  const calls = useCalls(id);
  const citable = useMemo(
    () => new Set([...(detail.data?.evidence ?? []), ...(detail.data?.suggestions ?? [])].map((item) => item.id)),
    [detail.data],
  );
  const highlighted = useCitationTarget(detail.data !== undefined);
  const open = detail.data !== undefined && isOpenStatus(detail.data.investigation.status);
  const stream = useInvestigationEvents(id, detail.data !== undefined, open);
  const steps = useMemo(() => stepIndex(detail.data?.plans ?? []), [detail.data]);
  const [focus, setFocus] = useState<StepFocus | null>(null);
  const selectStep = useCallback(
    (step: StepView, version: number) => setFocus({ stepId: step.id, version, at: Date.now() }),
    [],
  );

  if (detail.isPending) {
    return (
      <Layout crumb={id}>
        <Muted>正在加载…</Muted>
      </Layout>
    );
  }
  if (detail.error) {
    return (
      <Layout crumb={id}>
        <Section title={detail.error instanceof NotFoundError ? "没有这次调查" : "加载失败"} testId="load-error">
          <Muted>{detail.error instanceof NotFoundError ? `找不到调查 ${id}。` : detail.error.message}</Muted>
        </Section>
      </Layout>
    );
  }
  const data = detail.data;
  const info = data.investigation;
  const badges = (
    <>
      <HeaderChip>
        {info.llm_mode}
        {info.llm_model && ` · ${info.llm_model}`}
      </HeaderChip>
      <HeaderChip>{profileLabel(info.tool_backend)}</HeaderChip>
    </>
  );

  return (
    <CitableIds.Provider value={citable}>
      <Layout crumb={info.id} badges={badges}>
        <div className="space-y-5">
          <AlertCard detail={data} totals={calls.data?.totals} />
          <Section
            title="流程图"
            aside={<span className="text-xs text-slate-500 dark:text-slate-400">计划步骤按子 Agent 分泳道，颜色表示状态；点步骤看它在计划里的位置</span>}
            testId="flow"
          >
            {data.plans.length === 0 && <Muted>还没有计划。</Muted>}
            <FlowGraph plans={data.plans} onSelect={selectStep} />
            <ProgressFeed events={stream.events} state={stream.state} steps={steps} />
          </Section>
          <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
            <div className="min-w-0 space-y-5">
              <PlanPanel plans={data.plans} assessments={data.assessments} focus={focus} />
              {calls.data && (
                <CallTimeline calls={calls.data.calls} startedAt={info.started_at} elapsedMs={calls.data.totals.elapsed_ms} />
              )}
              <EvidenceList evidence={data.evidence} highlighted={highlighted} />
            </div>
            <div className="min-w-0 space-y-5">
              <ReportPanel report={data.report} rejected={data.rejected_attempts} />
              <SuggestionList investigationId={info.id} suggestions={data.suggestions} highlighted={highlighted} />
              <FollowupPanel
                investigationId={info.id}
                hasReport={data.report !== null}
                suggested={data.report?.body.followup_questions ?? []}
                references={data.report?.body.references ?? []}
              />
            </div>
          </div>
          {calls.data && <CallTable data={calls.data} startedAt={info.started_at} />}
          {calls.error && <Muted>调用记录加载失败：{calls.error.message}</Muted>}
        </div>
      </Layout>
    </CitableIds.Provider>
  );
}
