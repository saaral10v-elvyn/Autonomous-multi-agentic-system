import { HealthCheck } from "@/components/health-check";
export default function Home() {
  return <main className="mx-auto flex min-h-screen max-w-5xl flex-col px-6 py-8 sm:px-10">
    <header className="flex items-center justify-between"><a className="text-xl font-bold tracking-tight" href="/">Forge<span className="text-emerald-400">.</span></a><HealthCheck /></header>
    <section className="flex flex-1 flex-col justify-center py-24"><p className="mb-5 text-sm font-semibold uppercase tracking-[0.22em] text-emerald-400">Your idea, built end to end</p>
      <h1 className="max-w-3xl text-5xl font-semibold leading-tight tracking-tight sm:text-7xl">From first thought to <span className="text-emerald-400">live app.</span></h1>
      <p className="mt-7 max-w-xl text-lg leading-8 text-slate-300">A coordinated team of AI agents can plan, build, test, and deploy your web app—with you in control at every step.</p>
      <div className="mt-10"><a className="rounded-lg bg-emerald-400 px-5 py-3 font-semibold text-slate-950" href="#how-it-works">See how Forge works</a></div>
    </section>
    <section id="how-it-works" className="grid gap-6 border-t border-slate-800 py-8 text-sm text-slate-400 sm:grid-cols-3"><p><b className="text-slate-100">01</b> Share your idea</p><p><b className="text-slate-100">02</b> Review the plan</p><p><b className="text-slate-100">03</b> Ship with confidence</p></section>
  </main>;
}
