export const workspacePages = [
  { id: "overview", path: "/", label: "Overview", description: "Your paper account and research, in perspective.", tabs: [] },
  { id: "markets", path: "/markets", label: "Markets", description: "Prices, feed quality and market context.", tabs: [{id:"iex",label:"IEX feed"},{id:"consolidated",label:"Delayed SIP"},{id:"history",label:"Price history"},{id:"context",label:"Market context"}] },
  { id: "portfolio", path: "/portfolio", label: "Portfolio", description: "Broker records, positions and reconciled equity.", tabs: [{id:"ledger",label:"Account ledger"},{id:"equity",label:"Equity history"}] },
  { id: "learning", path: "/learning", label: "Learning", description: "Training cycles and forward evaluation evidence.", tabs: [{id:"cycles",label:"Learning cycles"},{id:"training",label:"Training"},{id:"evaluation",label:"Forward evaluation"},{id:"performance",label:"Performance"}] },
  { id: "research", path: "/research", label: "Research", description: "Research runs, strategy comparisons and model results.", tabs: [{id:"runs",label:"Research runs"},{id:"experiments",label:"Experiments"},{id:"library",label:"Strategies"},{id:"models",label:"Models"},{id:"signals",label:"Signals"},{id:"memory",label:"Memory"}] },
  { id: "risk", path: "/risk", label: "Risk & Safety", description: "Readiness, account controls and recovery evidence.", tabs: [{id:"readiness",label:"Readiness"},{id:"limits",label:"Risk limits"},{id:"broker",label:"Broker safety"},{id:"recovery",label:"Recovery"}] },
  { id: "activity", path: "/activity", label: "Activity", description: "Notifications and a traceable record of changes.", tabs: [{id:"notifications",label:"Notifications"},{id:"audit",label:"Audit history"}] },
  { id: "system", path: "/system", label: "System", description: "Service health, monitoring and workspace access.", tabs: [{id:"health",label:"Health"},{id:"monitoring",label:"Monitoring"},{id:"hardening",label:"Hardening"},{id:"live",label:"Live readiness"},{id:"access",label:"Access"}] },
];

export function resolveWorkspacePage(location: string) {
  const path = location.split(/[?#]/)[0].replace(/\/$/, "") || "/";
  if (["/sign-in", "/sign-up"].includes(path)) return {page: workspacePages[0], section: ""};
  const page = workspacePages.find(item => item.path === path || (item.path !== "/" && path.startsWith(item.path + "/")));
  if (!page) return null;
  const section = path === page.path ? page.tabs[0]?.id ?? "" : path.slice(page.path.length + 1);
  if (page.tabs.length && !page.tabs.some(tab => tab.id === section)) return null;
  return {page, section};
}
