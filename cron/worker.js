// The alarm clock for the site build.
//
// GitHub runs scheduled workflows on a best-effort basis, and for this repo that
// turned out to mean almost nothing: across October's first fortnight, 48% of the
// four-hourly slots never ran at all and the rest started a median 1h46m late.
// The build itself is fine on GitHub; only the trigger moves here, because
// Cloudflare cron triggers fire on time. This Worker's whole job is to ask GitHub
// to start build.yml via workflow_dispatch, on the same 7 */4 UTC schedule.
//
// Needs one secret, GITHUB_TOKEN: a fine-grained token scoped to this repo with
// Actions: read and write. Set it with `wrangler secret put GITHUB_TOKEN`.
// There is deliberately no fetch handler and no public URL — nothing outside
// Cloudflare's scheduler can make this Worker do anything.

async function dispatch(env) {
  const url = `https://api.github.com/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}/dispatches`;
  return fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "chinadesk-cron",
    },
    body: JSON.stringify({ ref: env.REF }),
  });
}

export default {
  async scheduled(controller, env, ctx) {
    if (!env.GITHUB_TOKEN) {
      throw new Error("GITHUB_TOKEN secret is not set — no build was triggered");
    }
    // One retry. A single transient 5xx from GitHub would otherwise cost a whole
    // four-hour cycle, which is the exact failure this Worker exists to remove.
    for (let attempt = 1; attempt <= 2; attempt++) {
      const res = await dispatch(env);
      if (res.status === 204) {
        console.log(`dispatched ${env.WORKFLOW} on ${env.REPO}@${env.REF} (cron ${controller.cron}, attempt ${attempt})`);
        return;
      }
      const body = (await res.text()).slice(0, 300);
      console.error(`dispatch failed: HTTP ${res.status} (attempt ${attempt}) ${body}`);
      // 401/403/404 are configuration problems; retrying won't fix them.
      if (res.status < 500) break;
      await new Promise((r) => setTimeout(r, 5000));
    }
    // Throwing marks the invocation as failed in the dashboard's Trigger Events.
    throw new Error("could not trigger the site build");
  },
};
