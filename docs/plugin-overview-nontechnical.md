> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE Plugins — What They Do
### A plain-language overview for non-technical colleagues

---

## The short version

NCE (Neuro-Cognitive Engine) connects to two major platforms your team already uses — **Microsoft Dynamics 365** and **NetBox** — and gives AI assistants a deep, live understanding of both. Instead of an AI that answers generic questions, you get one that actually knows your customers, your open cases, your network, and your infrastructure history.

---

## Dynamics 365 Plugin

**What is it?**
NCE hooks directly into your Dynamics 365 / Dataverse environment and continuously reads your CRM data — incidents, accounts, contacts, and opportunities. It turns that data into searchable, connected memory that an AI assistant can reason over.

**What can it do?**

| Capability | What it means in practice |
|---|---|
| **Live case lookup** | Ask the AI about any ticket by number and get the full picture: case details, all attached notes, activity history, and how it connects to other known issues. |
| **SLA breach monitoring** | NCE watches every incident against its SLA deadline and logs a tamper-proof record the moment a breach occurs. You get a chronological breach history you can query at any time — no manual tracking. |
| **Client stress tracking** | NCE reads the tone and language of case notes over time and builds a frustration curve per account. If a client's stress score passes a threshold across multiple incidents, it raises a burnout alert — so you can intervene proactively, before a customer escalates or churns. |
| **On-demand sync** | An operator or AI agent can trigger an immediate sync of all CRM entities (accounts, contacts, opportunities, incidents, work orders, agreements, customer assets, functional locations, knowledge articles) at any time, without waiting for the scheduled cycle. |
| **Per-tenant control** | The D365 integration can be enabled or disabled independently per tenant/team — useful for rollouts or isolating test environments. |

**Who benefits?**
Support leads, account managers, and operations teams. The AI stops being a generic chatbot and becomes a colleague that has actually read every case, remembers every SLA commitment, and notices when a client relationship is deteriorating.

---

## NetBox Plugin

**What is it?**
NCE connects to your NetBox instance — the source of truth for your physical and virtual infrastructure — and builds a cognitive map of your entire network: every site, rack, device, cable run, and circuit. It then does things with that map that NetBox alone cannot.

**What can it do?**

| Capability | What it means in practice |
|---|---|
| **Full infrastructure map** | NCE automatically pulls your complete topology — sites, racks, devices, connections — and turns it into a searchable, connected knowledge graph. Ask the AI "what devices are in rack 3 in Oslo?" and it knows. |
| **Unknown asset discovery** | NCE compares live telemetry against the official NetBox inventory. If something is talking on the network that isn't registered, NCE flags it and drafts the missing entry for review — reducing the gap between real infrastructure and documented infrastructure. |
| **Root-cause circuit escalation** | When a device failure is detected, NCE applies causal analysis (Pearl's do-calculus) to determine whether a specific circuit provider's degradation *actually caused* the failure — not just correlated with it. If the answer is yes, it auto-generates an upstream escalation ticket. No more manual "was it the ISP?" investigation. |
| **Cognitive dashboard inside NetBox** | A lightweight plugin for NetBox shows NCE's intelligence directly on device, rack, and site detail pages — stress trend, ahead-of-time fault map, real-time incident feed, active replay runs, and pending learning queue — without leaving the tool your team already uses. |
| **D365 bridge** | When a Dynamics 365 incident is linked to network infrastructure, NCE can cross-reference the two: "this customer case is about the same circuit that failed last Tuesday." The bridge connects your CRM and your network view automatically. |

**Who benefits?**
Network engineers, NOC teams, and infrastructure managers. Root-cause analysis that used to take an hour of manual correlation now surfaces in seconds. Unknown assets are found before they become a security or compliance problem. And your NetBox stays more accurate because NCE actively flags gaps.

---

## Together

When both plugins are active, NCE can connect the dots across domains that are normally siloed:

> *"The client in D365 has been escalating for two weeks. Their frustration score is high. The circuit serving their site had two provider-confirmed degradation events in that window. NCE flagged both — with evidence — and the escalation was drafted automatically."*

That kind of cross-domain awareness is what NCE is built for.

---

*For technical details, see `docs/d365_integration_reference.md` and `docs/netbox_and_cognitive_extensions.md`. Questions: ask the NCE team.*
