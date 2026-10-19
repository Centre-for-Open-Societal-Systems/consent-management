import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { AweTask } from "../api/types";

// Approver inbox. Tasks come from AWE via CM's proxy (forwarding the approver's
// JWT). Each task is a pending data-share-policy change; approving/rejecting is
// forwarded to AWE, and AWE's terminal webhook then activates or rejects the
// policy version in CM.
export default function ApprovalsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["awe-tasks"],
    // Open and claimed tasks: a claimed task still awaits this approver.
    queryFn: () => api.listMyTasks({ status: "actionable" }),
  });

  const tasks = data?.items ?? [];

  return (
    <div>
      <h1>Approvals</h1>
      <p className="muted" style={{ marginTop: -8, marginBottom: 24 }}>
        Data-share policy changes awaiting your approval. Approving here forwards your decision to
        the workflow engine; the policy takes effect once all required stages approve.
      </p>

      {isLoading && <div className="loading">Loading your tasks…</div>}
      {error && (
        <div className="notice notice-error">
          Could not load approval tasks. {error instanceof ApiError ? error.message : ""}
        </div>
      )}
      {data?.awe_enabled === false && (
        <div className="card">
          Approvals are turned off in this Consent Manager: policy changes take effect at once.
        </div>
      )}
      {data && data.awe_enabled !== false && tasks.length === 0 && (
        <div className="card">You have no pending approvals. 🎉</div>
      )}

      {tasks.map((t) => (
        <TaskCard key={t.id} task={t} />
      ))}
    </div>
  );
}

function ctxStr(ctx: Record<string, unknown> | null | undefined, key: string): string | undefined {
  const v = ctx?.[key];
  return typeof v === "string" ? v : v != null ? String(v) : undefined;
}
function ctxList(ctx: Record<string, unknown> | null | undefined, key: string): string[] {
  const v = ctx?.[key];
  return Array.isArray(v) ? v.map(String) : [];
}

function TaskCard({ task }: { task: AweTask }) {
  const qc = useQueryClient();
  const [comment, setComment] = useState("");
  const ctx = task.context ?? {};

  const label = ctxStr(ctx, "partner_label") ?? task.artifact_id ?? "policy change";
  const controller = ctxStr(ctx, "controller_id");
  const version = ctxStr(ctx, "policy_version");
  const scopes = ctxList(ctx, "allowed_data_scopes");
  const purposes = ctxList(ctx, "allowed_purposes");

  const decide = useMutation({
    mutationFn: (action: "approve" | "reject") =>
      api.submitTaskDecision(task.id, action, comment.trim() || undefined),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["awe-tasks"] }),
  });

  return (
    <div className="card">
      <div className="spread" style={{ marginBottom: 12 }}>
        <div>
          <h3 style={{ margin: 0 }}>{label}</h3>
          <div className="muted">
            Data-share policy change{version ? ` · v${version}` : ""}
            {controller ? ` · ${controller}` : ""} · stage {task.stage_order}
          </div>
        </div>
        <span className="badge badge-pending">{task.status}</span>
      </div>

      {scopes.length > 0 && (
        <div className="field" style={{ marginBottom: 12 }}>
          <label style={{ fontSize: 13 }}>Grants data scopes</label>
          <div className="chips">
            {scopes.map((s) => (
              <span key={s} className="chip selected">
                {s}
              </span>
            ))}
          </div>
        </div>
      )}
      {purposes.length > 0 && (
        <div className="field" style={{ marginBottom: 12 }}>
          <label style={{ fontSize: 13 }}>For purposes</label>
          <div className="chips">
            {purposes.map((s) => (
              <span key={s} className="chip selected">
                {s}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="field">
        <label style={{ fontSize: 13 }}>Comment (optional)</label>
        <input
          type="text"
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="Rationale for your decision"
        />
      </div>

      {decide.error && (
        <div className="notice notice-error">
          {decide.error instanceof ApiError ? decide.error.message : "Failed to submit decision."}
        </div>
      )}

      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button
          className="btn-danger"
          onClick={() => decide.mutate("reject")}
          disabled={decide.isPending}
        >
          Reject
        </button>
        <button
          className="btn-primary"
          onClick={() => decide.mutate("approve")}
          disabled={decide.isPending}
        >
          {decide.isPending ? "Submitting…" : "Approve"}
        </button>
      </div>
    </div>
  );
}
