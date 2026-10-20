import { useEffect, useMemo } from "react";
import { useLocation, useParams } from "react-router";
import { NotFoundError, useCalls, useInvestigation } from "../api/client";
import { AlertCard } from "../components/AlertCard";
import { CallTable } from "../components/CallTable";
import { CallTimeline } from "../components/CallTimeline";
import { EvidenceIds } from "../components/evidenceContext";
import { EvidenceList } from "../components/EvidenceList";
import { HeaderChip, Layout } from "../components/Layout";
import { PlanPanel } from "../components/PlanPanel";
import { ReportPanel } from "../components/ReportPanel";
import { SuggestionList } from "../components/SuggestionList";
import { Muted, Section } from "../components/ui";
import { profileLabel } from "../lib/format";

/** The evidence named by the URL hash (``#ev_…``): scrolled into view and highlighted. */
function useTargetEvidence(ready: boolean): string | null {
  const { hash } = useLocation();
  const target = ready && hash.startsWith("#ev_") ? decodeURIComponent(hash.slice(1)) : null;
  useEffect(() => {
    if (target !== null) document.getElementById(target)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [target, hash]);
  return target;
}

export function InvestigationDetailPage() {
  const { id = "" } = useParams();
  const detail = useInvestigation(id);
  const calls = useCalls(id);
  const evidenceIds = useMemo(() => new Set(detail.data?.evidence.map((e) => e.id) ?? []), [detail.data]);
  const highlighted = useTargetEvidence(detail.data !== undefined);

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
    <EvidenceIds.Provider value={evidenceIds}>
      <Layout crumb={info.id} badges={badges}>
        <div className="space-y-5">
          <AlertCard detail={data} totals={calls.data?.totals} />
          <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
            <div className="min-w-0 space-y-5">
              <PlanPanel plans={data.plans} assessments={data.assessments} />
              {calls.data && (
                <CallTimeline calls={calls.data.calls} startedAt={info.started_at} elapsedMs={calls.data.totals.elapsed_ms} />
              )}
              <EvidenceList evidence={data.evidence} highlighted={highlighted} />
            </div>
            <div className="min-w-0 space-y-5">
              <ReportPanel report={data.report} rejected={data.rejected_attempts} />
              <SuggestionList suggestions={data.suggestions} />
            </div>
          </div>
          {calls.data && <CallTable data={calls.data} startedAt={info.started_at} />}
          {calls.error && <Muted>调用记录加载失败：{calls.error.message}</Muted>}
        </div>
      </Layout>
    </EvidenceIds.Provider>
  );
}
