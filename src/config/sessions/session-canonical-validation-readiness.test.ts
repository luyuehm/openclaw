import { DatabaseSync } from "node:sqlite";
import { afterEach, expect, it, vi } from "vitest";
import {
  hasOpenClawAgentCanonicalValidation,
  invalidateOpenClawAgentDatabaseValidation,
} from "../../state/openclaw-agent-db-validation-cache.js";
import { openOpenClawAgentDatabase } from "../../state/openclaw-agent-db.js";
import { runOpenClawAgentWriteAdmission } from "../../state/openclaw-agent-write-admission.js";
import { withOpenClawTestState } from "../../test-utils/openclaw-test-state.js";
import * as archiveWorker from "./session-accessor.sqlite-archive.js";
import { certifySessionCanonicalValidationPending } from "./session-canonical-validation-readiness.js";
import { hasPendingCanonicalSessionValidation } from "./session-canonical-validation.js";

afterEach(() => vi.restoreAllMocks());

function seedPendingRows(count: number, textBytes = 0) {
  const options = { agentId: "main" };
  const database = openOpenClawAgentDatabase(options);
  const insert = database.db.prepare(`
    INSERT INTO session_nodes (session_key, current_session_id, entry_json, entry_valid, updated_at)
    VALUES (?, ?, ?, 1, 1)
  `);
  database.db.exec("BEGIN IMMEDIATE");
  try {
    for (let index = 0; index < count; index++) {
      const sessionId = `pending-${index}`;
      insert.run(
        `agent:main:${sessionId}`,
        sessionId,
        JSON.stringify({ sessionId, updatedAt: 1, lastRunError: "x".repeat(textBytes) }),
      );
    }
    database.db.exec("UPDATE session_nodes SET entry_valid = 1");
    database.db.exec("COMMIT");
  } catch (error) {
    database.db.exec("ROLLBACK");
    throw error;
  }
  return { options, database };
}

it("drains a large backlog in one retained worker while admitting foreground writes between batches", async () => {
  await withOpenClawTestState({ scenario: "minimal" }, async () => {
    const { options, database } = seedPendingRows(260, 16 * 1024);
    const before = database.db
      .prepare("SELECT session_key, entry_json FROM session_nodes ORDER BY session_key")
      .all();
    const events: string[] = [];
    let foreground: Promise<void> | undefined;
    const createWorker = archiveWorker.createSqliteTranscriptArchiveWorker;
    const started = vi
      .spyOn(archiveWorker, "createSqliteTranscriptArchiveWorker")
      .mockImplementation((data) => {
        const worker = createWorker(data);
        worker.on("message", (message: { type: string }) => {
          if (message.type !== "reclaimed") {
            return;
          }
          events.push("batch");
          foreground ??= runOpenClawAgentWriteAdmission(options, () => {
            events.push("foreground");
          });
        });
        return worker;
      });
    await certifySessionCanonicalValidationPending(options);
    await foreground;
    expect(started).toHaveBeenCalledOnce();
    expect(events[0]).toBe("batch");
    expect(events.indexOf("foreground")).toBeGreaterThan(0);
    expect(events.lastIndexOf("batch")).toBeGreaterThan(events.indexOf("foreground"));
    expect(hasPendingCanonicalSessionValidation(database)).toBe(false);
    expect(
      database.db
        .prepare("SELECT session_key, entry_json FROM session_nodes ORDER BY session_key")
        .all(),
    ).toEqual(before);
  });
});

it("retains a changed row's marker instead of certifying its stale worker snapshot", async () => {
  await withOpenClawTestState({ scenario: "minimal" }, async () => {
    const { options, database } = seedPendingRows(1);
    let changed = false;
    let markerRetainedAfterFirstBatch = false;
    const createWorker = archiveWorker.createSqliteTranscriptArchiveWorker;
    vi.spyOn(archiveWorker, "createSqliteTranscriptArchiveWorker").mockImplementation((data) => {
      const worker = createWorker(data);
      worker.on("message", (message: { type: string }) => {
        if (message.type === "admission-request" && !changed) {
          changed = true;
          database.db.exec("UPDATE session_nodes SET parent_session_key = 'agent:main:changed'");
        } else if (message.type === "reclaimed") {
          markerRetainedAfterFirstBatch = hasPendingCanonicalSessionValidation(database);
        }
      });
      return worker;
    });
    await expect(certifySessionCanonicalValidationPending(options)).rejects.toThrow(
      "invalid persisted session row",
    );
    expect(changed).toBe(true);
    expect(markerRetainedAfterFirstBatch).toBe(true);
    expect(hasPendingCanonicalSessionValidation(database)).toBe(true);
    expect(database.db.prepare("SELECT parent_session_key FROM session_nodes").get()).toEqual({
      parent_session_key: "agent:main:changed",
    });
  });
});

it.each([false, true])(
  "fully validates a copied populated store whose pending table is clean (invalid row: %s)",
  async (invalid) => {
    await withOpenClawTestState({ scenario: "minimal" }, async (state) => {
      const { options, database } = seedPendingRows(130);
      await certifySessionCanonicalValidationPending(options);
      const copiedPath = state.statePath("copied-agent.sqlite");
      database.db.prepare("VACUUM INTO ?").run(copiedPath);
      if (invalid) {
        const imported = new DatabaseSync(copiedPath);
        try {
          // Untrusted copied derived state cannot certify its own source contents.
          imported.exec(
            "UPDATE session_nodes SET parent_session_key = 'agent:main:changed' WHERE session_key = 'agent:main:pending-0'",
          );
          imported.exec("DELETE FROM session_canonical_validation_pending");
        } finally {
          imported.close();
        }
      }
      const copiedOptions = { ...options, path: copiedPath };
      const copied = openOpenClawAgentDatabase(copiedOptions);
      expect(hasPendingCanonicalSessionValidation(copied)).toBe(false);
      expect(hasOpenClawAgentCanonicalValidation(copied)).toBe(false);
      const result = certifySessionCanonicalValidationPending(copiedOptions);
      if (invalid) {
        await expect(result).rejects.toThrow("invalid persisted session row");
        expect(hasOpenClawAgentCanonicalValidation(copied)).toBe(false);
        expect(hasPendingCanonicalSessionValidation(copied)).toBe(true);
      } else {
        await result;
        expect(hasOpenClawAgentCanonicalValidation(copied)).toBe(true);
        expect(hasPendingCanonicalSessionValidation(copied)).toBe(false);
      }
    });
  },
);

it("refuses to publish canonical readiness after its physical verification receipt is revoked", async () => {
  await withOpenClawTestState({ scenario: "minimal" }, async () => {
    const { options, database } = seedPendingRows(1);
    const createWorker = archiveWorker.createSqliteTranscriptArchiveWorker;
    vi.spyOn(archiveWorker, "createSqliteTranscriptArchiveWorker").mockImplementation((data) => {
      const worker = createWorker(data);
      worker.on("message", (message: { type: string }) => {
        if (message.type === "reclaimed") {
          invalidateOpenClawAgentDatabaseValidation(database.path);
        }
      });
      return worker;
    });
    await expect(certifySessionCanonicalValidationPending(options)).rejects.toThrow(
      "database owner is no longer current",
    );
    expect(hasOpenClawAgentCanonicalValidation(database)).toBe(false);
  });
});

it("terminates a drain that stalls every batch under an active writer and publishes readiness", async () => {
  await withOpenClawTestState({ scenario: "minimal" }, async () => {
    const { options, database } = seedPendingRows(1);
    // Flip the row's entry_json between the read and write halves of every
    // certify batch while keeping entry_valid settled at 1. The snapshot
    // captured by readPendingCanonicalSessionValidationBatch no longer matches
    // compareAndCertifyCanonicalSessionValidationBatch's reread, so
    // certifiedRows stays 0 while hasMore stays true. The readiness owner must
    // cap the stalled batches and publish canonicalReady instead of looping
    // forever; the row remains pending for the next request to drain.
    let mutations = 0;
    const createWorker = archiveWorker.createSqliteTranscriptArchiveWorker;
    vi.spyOn(archiveWorker, "createSqliteTranscriptArchiveWorker").mockImplementation((data) => {
      const worker = createWorker(data);
      worker.on("message", (message: { type: string }) => {
        if (message.type === "admission-request") {
          mutations += 1;
          // Emulate an active writer's two-step commit between the read and
          // write halves: change entry_json (which clears entry_valid via the
          // entry_json projection trigger) and then settle entry_valid back to
          // 1. The row stays valid, but the entry_json captured by the read
          // snapshot no longer matches the write-half reread, so certifiedRows
          // stays 0 while hasMore stays true.
          database.db.exec(
            `UPDATE session_nodes SET entry_json = '{"sessionId":"pending-0","updatedAt":1,"label":"mut-${mutations}"}' WHERE session_key = 'agent:main:pending-0'`,
          );
          database.db.exec(
            "UPDATE session_nodes SET entry_valid = 1 WHERE session_key = 'agent:main:pending-0'",
          );
        }
      });
      return worker;
    });
    await expect(certifySessionCanonicalValidationPending(options)).resolves.toBeUndefined();
    expect(mutations).toBeGreaterThan(0);
    expect(hasPendingCanonicalSessionValidation(database)).toBe(true);
    expect(hasOpenClawAgentCanonicalValidation(database)).toBe(true);
  });
});

it("does not re-pend a clean row on a bare entry_json content edit", async () => {
  await withOpenClawTestState({ scenario: "minimal" }, async () => {
    const { options, database } = seedPendingRows(1);
    // Drain the seeded pending marker so the row is certified and clean.
    await certifySessionCanonicalValidationPending(options);
    expect(hasPendingCanonicalSessionValidation(database)).toBe(false);
    // Drop entry_valid to 0 and clear the pending marker so the row is settled
    // but validity is open. A subsequent bare entry_json edit cannot ride an
    // entry_valid transition back into pending, and after the fix the canonical
    // pending trigger no longer fires on entry_json at all.
    database.db.exec(
      "UPDATE session_nodes SET entry_valid = 0 WHERE session_key = 'agent:main:pending-0'",
    );
    database.db.exec("DELETE FROM session_canonical_validation_pending");
    expect(hasPendingCanonicalSessionValidation(database)).toBe(false);
    database.db
      .prepare("UPDATE session_nodes SET entry_json = ? WHERE session_key = ?")
      .run(
        JSON.stringify({ sessionId: "pending-0", updatedAt: 2, label: "content-only" }),
        "agent:main:pending-0",
      );
    // entry_valid stays 0 (the entry_valid_after_entry_update trigger is a no-op
    // when it is already 0), so no canonical pending transition fires.
    expect(hasPendingCanonicalSessionValidation(database)).toBe(false);
    // The writer's normal commit step flips entry_valid to 1; that canonical
    // transition re-pends the row so the next request re-validates its shape.
    database.db.exec(
      "UPDATE session_nodes SET entry_valid = 1 WHERE session_key = 'agent:main:pending-0'",
    );
    expect(hasPendingCanonicalSessionValidation(database)).toBe(true);
  });
});

it("converges a drain while sessions are actively written between batches", async () => {
  await withOpenClawTestState({ scenario: "minimal" }, async () => {
    const { options, database } = seedPendingRows(4, 256);
    // Simulate an active writer flipping entry_json on a sibling session
    // between batches. Before the fix this kept certifiedRows=0 every batch
    // and the drain never returned; now it caps stalled batches and publishes.
    let writes = 0;
    const createWorker = archiveWorker.createSqliteTranscriptArchiveWorker;
    vi.spyOn(archiveWorker, "createSqliteTranscriptArchiveWorker").mockImplementation((data) => {
      const worker = createWorker(data);
      worker.on("message", (message: { type: string }) => {
        if (message.type === "reclaimed" && writes < 3) {
          writes += 1;
          database.db.exec(
            `UPDATE session_nodes SET entry_json = '{"sessionId":"pending-1","updatedAt":${writes}}' WHERE session_key = 'agent:main:pending-1'`,
          );
        }
      });
      return worker;
    });
    await expect(certifySessionCanonicalValidationPending(options)).resolves.toBeUndefined();
    expect(hasOpenClawAgentCanonicalValidation(database)).toBe(true);
  });
});
