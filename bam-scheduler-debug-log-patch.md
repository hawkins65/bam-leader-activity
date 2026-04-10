# BAM Scheduler Debug Log Patch

Adds a debug log to `bam_scheduler.rs` so bundle transaction signatures are
logged during BAM processing — matching the format used by `bundle_stage.rs:930`
so the existing `bundle-txn-signatures.py` extraction script works unchanged.

## Problem

BAM bundles flow through `BankingStage` → `BamScheduler` → `ConsumeWorker`, NOT
through the legacy `BundleStage`. The debug log at `bundle_stage.rs:930`:

```
execution results: bundle signatures: [...], result: ...
```

is never hit for BAM bundles because they take a completely different code path.
The `set-log-filter` for `solana_core::bundle_stage=debug` does nothing for BAM.

## Patch

File: `~/jito-solana/core/src/banking_stage/transaction_scheduler/bam_scheduler.rs`

### Step 1: Add SVMTransaction import (line 42)

```diff
     solana_svm_transaction::svm_message::SVMMessage,
+    solana_svm_transaction::svm_transaction::SVMTransaction,
     solana_transaction_error::TransactionError,
```

### Step 2: Capture signatures before work recycling (line 731-735)

The work object (containing transactions) is recycled at line 735 via
`recycle_work_object()`, which clears the transactions. Signatures must be
captured before that call.

```diff
         while let Ok(result) = self.finished_consume_work_receiver.try_recv() {
             num_transactions += result.work.ids.len();
             let batch_id = result.work.batch_id;
             let revert_on_error = result.work.revert_on_error;
+
+            // Capture signatures before recycling (which clears transactions)
+            let bundle_signatures: Vec<_> = if log::log_enabled!(log::Level::Debug) {
+                result.work.transactions.iter().map(|tx| tx.signature().to_string()).collect()
+            } else {
+                Vec::new()
+            };
+
             self.recycle_work_object(result.work);
```

### Step 3: Add debug log after bundle_result is generated (line 771-772)

Inside the per-transaction loop, after `bundle_result` is computed:

```diff
                     Self::generate_bundle_result(txn_result)
                 };
+
+                debug!(
+                    "execution results: bundle signatures: {:?}, result: {:?}",
+                    bundle_signatures, bundle_result
+                );
+
                 self.send_back_result(priority_to_seq_id(priority_id.priority), bundle_result);
```

## Build

```bash
cd ~/jito-solana
cargo build --release 2>&1 | tail -5
# Takes ~15-30 minutes
```

Binary: `~/jito-solana/target/release/agave-validator`

## Deploy

```bash
# Stop validator
sudo systemctl stop sol

# Swap binary
cp ~/jito-solana/target/release/agave-validator \
   ~/.local/share/solana/install/active_release/bin/agave-validator

# Restart
sudo systemctl start sol

# Watch startup
tail -f ~/logs/validator.log | grep -E "identity|slot|error"
```

## Enable debug logging

The BAM scheduler module path is:
`solana_core::banking_stage::transaction_scheduler::bam_scheduler`

```bash
# Enable (targets both BAM and legacy paths)
agave-validator -l /mnt/ledger set-log-filter \
  "solana=info,solana_core::banking_stage::transaction_scheduler::bam_scheduler=debug,solana_core::bundle_stage=debug"

# Restore
agave-validator -l /mnt/ledger set-log-filter "solana=info,agave=info"
```

Update `capture-bundle-txns.sh` if still used:
```bash
DEBUG_FILTER="solana=info,solana_core::bundle_stage=debug,solana_core::banking_stage::transaction_scheduler::bam_scheduler=debug"
```

## Verify

1. Enable debug logging (see above)
2. Wait for next leader slot: `solana leader-schedule | grep Tri1F8B6`
3. Check for output:
   ```bash
   grep "execution results: bundle signatures:" ~/logs/validator.log | head -5
   ```
4. Run extraction:
   ```bash
   python3 ~/bam-leader-activity/bundle-txn-signatures.py ~/logs/validator.log
   ```
5. Restore default logging

## Notes

- `log::log_enabled!(log::Level::Debug)` guard ensures zero overhead when
  debug logging is off — no signature collection or formatting occurs.
- The log format matches the old BundleStage format exactly, so
  `bundle-txn-signatures.py` regex works without modification.
- `bundle_signatures` captures all transaction signatures in the batch.
  For `revert_on_error` batches this is the full bundle; for non-revert
  batches it's typically one transaction per batch.
