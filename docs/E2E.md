# Manual End-to-End Checklist (M8)

Use a non-production test GitHub repository and Vercel project. Connect both integrations and a test LLM provider before starting. The flow may create a repository, push a branch, and deploy; approve only when the approval card shows the intended account and target.

## Shared test idea

> Build a responsive reading list app with a title, URL, optional notes, status (to read/reading/done), filtering, and local persistence. Include a clear empty state and a short README. Keep it free-tier compatible.

For each autonomy preset, start a new run with the same idea and a low test budget. The clarifier should ask a small batch of questions, the planner should create an editable plan, tasks should run in the sandbox, and the Deployer should publish only after the Reviewer approves the implementation. The completed run should have a Markdown report with test results, cost, approvals, repository URL, and live URL.

## Step-by-step

- Confirm the plan approval is required and review/edit the plan.
- Confirm sandbox writes and reversible external changes request approval.
- Confirm deployment requests approval, even after repository work is approved.
- Approve deployment and verify the live URL returns HTTP 200 and the app works.
- Open the final report, download the Markdown, and use “Iterate on this project.”
- Confirm the follow-up run uses the same project and includes prior repository context.

## Milestones

- Confirm the plan approval is required.
- Confirm sandbox writes, branch work, and pull request changes proceed without per-action approval.
- Confirm deployment still requires approval.
- Verify the live app, report contents, Markdown download, and follow-up project iteration.

## Autopilot

- Confirm the plan is shown and automatically accepted.
- Confirm sandbox writes and reversible external changes proceed automatically.
- Confirm deployment still pauses for explicit approval.
- Verify the live app, report contents, Markdown download, and follow-up project iteration.

## Recovery

- Use a disposable run with a planned task that can be made to fail.
- Confirm the run shows failed or blocked tasks and offers “Retry from this step.”
- Retry the failing task; confirm dependent tasks are reset and prior completed tasks remain complete.
- Pause during work, resume, and confirm the run continues without duplicating completed tool side effects.

## Evidence to record

- Run and project IDs, autonomy preset, repository URL, live URL.
- Approval cards and outcomes for deployment.
- Test summary and report download.
- Any failed task recovery or iteration issues.
