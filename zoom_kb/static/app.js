const state = {view: "dashboard", channels: [], syncSettings: {initial_sync_days: 90, allowed_days: [30, 90, 180]}};
const content = document.querySelector("#content");
const notice = document.querySelector("#notice");
const syncButton = document.querySelector("#syncButton");
const progressStatus = document.querySelector("#progressStatus");
let progressTimer = null;
const topicLanguageBuffer = new Map();
const esc = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const localDateFormatter = new Intl.DateTimeFormat(undefined, {year:"numeric",month:"short",day:"numeric",hour:"numeric",minute:"2-digit",timeZoneName:"short"});
function fmtDate(value) { if(!value)return "—";const date=new Date(value);return Number.isNaN(date.getTime())?String(value):localDateFormatter.format(date); }

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}
function show(message, error = false) { notice.textContent = message; notice.className = `notice${error ? " error" : ""}`; setTimeout(() => notice.classList.add("hidden"), 7000); }
function startProgress(title, detail) {
  const started = Date.now();
  document.querySelector("#progressTitle").textContent = title;
  document.querySelector("#progressDetail").textContent = detail;
  const update = () => { const seconds=Math.floor((Date.now()-started)/1000);document.querySelector("#progressElapsed").textContent=`Elapsed ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,"0")}`; };
  update();
  clearInterval(progressTimer);
  progressTimer = setInterval(update, 1000);
  progressStatus.classList.remove("hidden");
}
function stopProgress() { clearInterval(progressTimer);progressTimer=null;progressStatus.classList.add("hidden"); }
function badge(status) { return `<span class="badge ${esc(status)}">${esc(status)}</span>`; }

const meta = {
  dashboard: ["Overview", "Zoom Chat sync status and local coverage"],
  channels: ["Channels & Readability", "Choose channels and verify whether message text is available"],
  topics: ["Conversation Topics", "AI turns related Zoom messages into traceable business topics"],
  knowledge: ["Knowledge Base", "Automatically archived, reusable knowledge with original Zoom messages preserved"],
  mentions: ["Mentions", "Local follow-up list without changing Zoom read state"],
  settings: ["Settings", "Configure identity matching, AI model tier, query language, and knowledge maturity"],
};

async function render(view = state.view) {
  state.view = view;
  document.querySelectorAll("nav button").forEach(button => button.classList.toggle("active", button.dataset.view === view));
  document.querySelector("#title").textContent = meta[view][0];
  document.querySelector("#subtitle").textContent = meta[view][1];
  syncButton.hidden = view === "settings";
  content.innerHTML = "<div class='panel empty'>Loading…</div>";
  try { await ({dashboard, channels, topics, knowledge, mentions, settings}[view])(); }
  catch (error) { content.innerHTML = `<div class="panel empty">${esc(error.message)}</div>`; show(error.message, true); }
}

async function dashboard() {
  const data = await api("/api/dashboard");
  const channels = data.channels || {};
  content.innerHTML = `<div class="cards">
    <div class="card"><div class="label">Active channels</div><div class="value">${Object.values(channels).reduce((a,b)=>a+b,0)}</div></div>
    <div class="card"><div class="label">Selected for sync</div><div class="value">${data.selected_channels}</div></div>
    <div class="card"><div class="label">Local messages</div><div class="value">${data.messages}</div></div>
    <div class="card"><div class="label">Open mentions</div><div class="value">${data.open_mentions}</div></div>
  </div><div class="panel"><div class="panel-head"><h2>Readability distribution</h2></div><table><tbody>
    ${Object.entries(channels).map(([key,value])=>`<tr><td>${badge(key)}</td><td>${value} channels</td></tr>`).join("") || "<tr><td class='empty'>No channels scanned yet</td></tr>"}
  </tbody></table></div><div class="panel"><h2>Latest sync</h2><p class="muted">${data.last_run ? `${esc(data.last_run.status)} · ${data.last_run.message_count} messages · ${esc(fmtDate(data.last_run.finished_at || data.last_run.started_at))}` : "Not run yet"}</p></div>`;
}

async function channels() {
  [state.channels, state.syncSettings] = await Promise.all([api("/api/channels"), api("/api/settings/sync")]);
  const days = state.syncSettings.initial_sync_days;
  content.innerHTML = `<div class="panel"><div class="panel-head"><div><h2>Initial sync range for new channels</h2><p class="muted">Only affects channels that have not completed their first sync. Existing data is never deleted or automatically backfilled.</p></div>
    <select id="initialSyncDays">${state.syncSettings.allowed_days.map(day=>`<option value="${day}" ${day===days?"selected":""}>${day} days${day===90?" (default)":""}</option>`).join("")}</select></div>
    <div class="toolbar"><input id="channelSearch" placeholder="Search channels"><div class="actions"><button class="secondary" id="refreshChannels">Refresh from Zoom</button><button class="secondary" id="probeSelected">Test Selected Channels</button></div></div><div id="channelTable"></div></div>`;
  drawChannels();
  document.querySelector("#channelSearch").oninput = drawChannels;
  document.querySelector("#initialSyncDays").onchange = async event => { const days=Number(event.target.value);await api("/api/settings/sync",{method:"PUT",body:JSON.stringify({days})});state.syncSettings.initial_sync_days=days;drawChannels();show(`Initial sync range set to ${days} days. Existing data was not changed.`); };
  document.querySelector("#refreshChannels").onclick = event => work(event.target, async()=>{const result=await api("/api/channels/refresh",{method:"POST"});show(`Refreshed ${result.count} channels`);await channels();});
  document.querySelector("#probeSelected").onclick = event => work(event.target, async()=>{const ids=state.channels.filter(c=>c.selected).map(c=>c.id);if(!ids.length)throw new Error("Select at least one channel first.");const result=await api("/api/channels/probe",{method:"POST",body:JSON.stringify({channel_ids:ids,days:30})});show(`Tested ${result.count} channels`);await channels();});
}

function syncStage(stage) { return ({not_started:"Not synced",initial_failed:"Initial sync failed",incremental:"Incremental sync",incremental_failed:"Incremental sync failed"})[stage] || stage; }
function needsBackfill(channel) { if(!channel.initial_sync_completed_at)return false;const covered=channel.covered_from_at?new Date(channel.covered_from_at):null;const target=new Date(Date.now()-state.syncSettings.initial_sync_days*86400000);return !covered||covered>target; }
function drawChannels() {
  const query = (document.querySelector("#channelSearch")?.value || "").toLowerCase();
  const rows = state.channels.filter(channel => channel.name.toLowerCase().includes(query));
  document.querySelector("#channelTable").innerHTML = `<table><thead><tr><th>Sync</th><th>Channel</th><th>Message text</th><th>Sync stage / coverage</th><th>Local messages</th><th>Action</th></tr></thead><tbody>${rows.map(channel=>`<tr>
    <td><input type="checkbox" data-id="${esc(channel.id)}" ${channel.selected?"checked":""}></td><td><b>${esc(channel.name)}</b><br><span class="muted">${esc(channel.readability_reason||"")}</span></td>
    <td>${badge(channel.readability_status)}</td><td>${esc(syncStage(channel.sync_stage))}<br><span class="muted">${channel.covered_from_at?`Covered since ${esc(fmtDate(channel.covered_from_at))}`:"No coverage record"}${channel.last_error?`<br>${esc(channel.last_error)}`:""}</span></td>
    <td>${channel.message_count}</td><td>${needsBackfill(channel)?`<button class="secondary" data-backfill="${esc(channel.id)}">Backfill to ${state.syncSettings.initial_sync_days} days</button>`:`<span class="muted">${esc(fmtDate(channel.last_success_at||channel.last_sync_at))}</span>`}</td></tr>`).join("")}</tbody></table>`;
  document.querySelectorAll("#channelTable input[type=checkbox]").forEach(box=>box.onchange=async()=>{await api(`/api/channels/${encodeURIComponent(box.dataset.id)}`,{method:"PATCH",body:JSON.stringify({selected:box.checked})});const item=state.channels.find(c=>c.id===box.dataset.id);if(item)item.selected=box.checked;});
  document.querySelectorAll("[data-backfill]").forEach(button=>button.onclick=event=>work(event.target,async()=>{const days=state.syncSettings.initial_sync_days;if(!confirm(`Backfill this channel to the last ${days} days? Existing messages will be preserved and deduplicated.`))return;const result=await api(`/api/channels/${encodeURIComponent(button.dataset.backfill)}/backfill`,{method:"POST",body:JSON.stringify({days})});show(result.changed?`Backfill complete: ${result.messages} messages inserted or updated.`:"This channel already covers the selected range.");await channels();}));
}

async function topics() {
  const [rows, ai, knowledgeSettings] = await Promise.all([api("/api/topics"), api("/api/ai/status"), api("/api/settings/knowledge")]);
  content.innerHTML = `<div class="panel"><div class="panel-head"><div><h2>Recent conversation topics</h2><p class="muted">Topics remain here for ${knowledgeSettings.maturity_days} days, then are automatically summarized in their source language and archived to the Knowledge Base.</p><p class="muted">${ai.configured?`${esc(ai.model_tier)} tier · ${esc(ai.model)}`:"OpenAI API is not configured"}</p></div><div class="actions">
    <button class="primary" id="analyzeTopics" ${ai.configured?"":"disabled"}>Extract New Topics</button></div></div>
    <div class="toolbar"><input id="topicSearch" placeholder="Search titles, problems, or discussions"><button class="secondary" id="searchTopics">Search</button></div><div id="topicList">${topicRows(rows)}</div></div>`;
  document.querySelector("#searchTopics").onclick=async()=>{const query=document.querySelector("#topicSearch").value;document.querySelector("#topicList").innerHTML=topicRows(await api(`/api/topics?search=${encodeURIComponent(query)}`));bindTopics();};
  document.querySelector("#analyzeTopics").onclick=event=>work(event.target,async()=>{const result=await api("/api/topics/analyze",{method:"POST",body:JSON.stringify({max_windows:8})});show(`Processed ${result.processed} windows across the ${result.analysis_days}-day maturity period, created ${result.topics} topics, skipped ${result.skipped} unchanged windows${result.errors.length?`, ${result.errors.length} errors`:""}`);await topics();},{label:"Extracting…",title:"Extracting conversation topics",detail:`Reading messages inside the ${knowledgeSettings.maturity_days}-day knowledge maturity period and calling the configured AI model.`});
  bindTopics();
}

function topicRows(rows) {
  rows.forEach(topic=>topicLanguageBuffer.set(`${topic.id}:${topicLanguage(topic)}`,topic));
  return rows.map(topic=>`<article class="panel topic-card" style="box-shadow:none"><div class="panel-head"><div>${badge(topic.status)} <span class="muted">${Math.round(topic.confidence*100)}% confidence</span></div><span class="muted">${esc(topic.channel_name)} · ${esc(fmtDate(topic.last_message_at))}</span></div>
    <h2>${esc(topic.title)}</h2><p><b>Problem:</b> ${esc(topic.problem_summary||"—")}</p><p><b>Discussion:</b> ${esc(topic.discussion_summary||"—")}</p>
    <div data-topic-conclusions>${topicListBlock("Conclusions:",topic.conclusions)}</div><div data-topic-questions>${topicListBlock("Open questions:",topic.open_questions)}</div>
    <div class="actions"><button class="secondary" data-topic-detail="${topic.id}">View Original Messages (${topic.source_message_count})</button><button class="topic-action" data-topic-switch-language="${topic.id}" data-current-language="${topicLanguage(topic)}" title="Switch between English and Chinese">Switch Language</button><button class="topic-action" data-topic-search-kb="${topic.id}">Search KB</button><button class="topic-action" data-topic-ignore="${topic.id}">Do not track</button></div><div id="topicSearchResults${topic.id}"></div><div id="topicSources${topic.id}"></div></article>`).join("") || "<div class='empty'>No recent topics. Sync messages, then select Extract New Topics.</div>";
}
function topicLanguage(topic){return topic.source_language==="chinese"||(/[\u4e00-\u9fff]/.test(`${topic.title} ${topic.problem_summary}`)&&topic.source_language!=="english")?"chinese":"english";}
function topicListBlock(label,items){return items?.length?`<div><b>${label}</b><ul>${items.map(item=>`<li>${esc(item)}</li>`).join("")}</ul></div>`:"";}
function applyTopicLanguage(card,topic,language){const paragraphs=Array.from(card.children).filter(item=>item.tagName==="P");card.querySelector("h2").textContent=topic.title||"";paragraphs[0].innerHTML=`<b>Problem:</b> ${esc(topic.problem_summary||"—")}`;paragraphs[1].innerHTML=`<b>Discussion:</b> ${esc(topic.discussion_summary||"—")}`;card.querySelector("[data-topic-conclusions]").innerHTML=topicListBlock("Conclusions:",topic.conclusions);card.querySelector("[data-topic-questions]").innerHTML=topicListBlock("Open questions:",topic.open_questions);card.querySelector("[data-topic-switch-language]").dataset.currentLanguage=language;}
function sourceList(rows) { return `<div class="source-list"><p class="muted">Original Zoom Chat text is preserved verbatim for evidence and traceability.</p>${rows.map(source=>`<div class="source-item"><b>${esc(source.sender_name||"—")}</b> <span class="muted">${esc(source.channel_name||"")} ${esc(fmtDate(source.sent_at))}</span><div>${esc(source.body)}</div></div>`).join("")}</div>`; }
function bindTopics() {
  document.querySelectorAll("[data-topic-switch-language]").forEach(button=>button.onclick=event=>work(event.target,async()=>{const id=button.dataset.topicSwitchLanguage;const target=button.dataset.currentLanguage==="chinese"?"english":"chinese";const key=`${id}:${target}`;let translated=topicLanguageBuffer.get(key);let cached=true;if(!translated){const result=await api(`/api/topics/${id}/translate`,{method:"POST",body:JSON.stringify({target_language:target})});translated=result.translation;cached=result.cached;topicLanguageBuffer.set(key,translated);}applyTopicLanguage(button.closest(".topic-card"),translated,target);show(`Showing ${target==="chinese"?"Chinese":"English"}${cached?" (cached)":""}. Original Zoom messages are unchanged.`);},{label:"Translating…",title:"Translating conversation topic",detail:"Translating the topic summary while keeping original Zoom messages unchanged."}));
  document.querySelectorAll("[data-topic-search-kb]").forEach(button=>button.onclick=event=>work(event.target,async()=>{const id=button.dataset.topicSearchKb;const target=document.querySelector(`#topicSearchResults${id}`);const rows=await api(`/api/topics/${id}/search-knowledge`);target.innerHTML=`<div class="search-results"><h3>Related knowledge</h3>${knowledgeMatches(rows)}</div>`;bindKnowledge();},{label:"Searching…",title:"Searching the Knowledge Base",detail:"Translating the query when needed and matching it against knowledge stored in its original language."}));
  document.querySelectorAll("[data-topic-ignore]").forEach(button=>button.onclick=event=>work(event.target,async()=>{if(!confirm("Stop tracking this topic? It will stay hidden when matching messages are analyzed again."))return;await api(`/api/topics/${button.dataset.topicIgnore}/ignore`,{method:"POST"});show("Topic will no longer be tracked");await topics();}));
  document.querySelectorAll("[data-topic-detail]").forEach(button=>button.onclick=event=>work(event.target,async()=>{const id=button.dataset.topicDetail;const target=document.querySelector(`#topicSources${id}`);if(target.dataset.loaded){target.innerHTML="";delete target.dataset.loaded;return;}const topic=await api(`/api/topics/${id}`);target.dataset.loaded="1";target.innerHTML=sourceList(topic.sources);}));
}

async function knowledge() {
  const [rows, importStatus, settings] = await Promise.all([api("/api/knowledge/recent?view=added"),api("/api/knowledge/bootstrap/status"),api("/api/settings/knowledge")]);
  content.innerHTML = `<div class="panel"><div class="panel-head"><div><h2>Knowledge Base</h2><p class="muted">Knowledge is archived automatically in the original conversation language. Search works across Chinese and English.</p><p class="muted">Historical import: ${esc(importStatus.status)} · ${importStatus.remaining_windows ?? 0} remaining · ${importStatus.processed_windows||0} processed · ${importStatus.identified_topics||0} topics · ${importStatus.knowledge_created||0} created · ${importStatus.knowledge_updated||0} updated · ${importStatus.error_count||0} failed · ${importStatus.elapsed_seconds||0}s</p></div><button class="primary" id="importHistorical">Continue Historical Import</button></div>
    <div class="toolbar"><input id="knowledgeSearch" placeholder="Search in Chinese or English"><button class="secondary" id="searchKnowledge">Search</button></div>
    <div class="view-tabs"><button class="secondary active" data-kview="added">Recently added</button><button class="secondary" data-kview="updated">Recently updated</button><button class="secondary" data-kview="unresolved">Unresolved</button><button class="secondary" data-kview="activity">New activity</button></div><div id="knowledgeList">${knowledgeRows(rows)}</div></div>`;
  document.querySelector("#searchKnowledge").onclick=async()=>{const query=document.querySelector("#knowledgeSearch").value.trim();if(!query)return;document.querySelector("#knowledgeList").innerHTML=knowledgeRows(await api("/api/knowledge/search",{method:"POST",body:JSON.stringify({query,limit:30})}));bindKnowledge();};
  document.querySelectorAll("[data-kview]").forEach(button=>button.onclick=async()=>{document.querySelectorAll("[data-kview]").forEach(x=>x.classList.remove("active"));button.classList.add("active");document.querySelector("#knowledgeList").innerHTML=knowledgeRows(await api(`/api/knowledge/recent?view=${button.dataset.kview}`));bindKnowledge();});
  document.querySelector("#importHistorical").onclick=event=>work(event.target,async()=>{const result=await api("/api/knowledge/bootstrap",{method:"POST",body:JSON.stringify({max_windows:8})});show(`Historical import ${result.status}: ${result.processed} processed, ${result.created} created, ${result.updated} updated, ${result.related} related, ${result.conflicts} conflicts, ${result.skipped} skipped${result.errors.length?`, ${result.errors.length} errors`:""}.`);await knowledge();},{label:"Building…",title:"Building historical knowledge",detail:"Scanning messages, extracting source-language topics, generating embeddings, and safely creating or updating knowledge."});
  bindKnowledge();
}
function knowledgeRows(rows) {
  return rows.map(item=>`<article class="knowledge-row"><div><h2>${esc(item.canonical_question)}</h2><p class="muted">${esc(item.source_language)} · ${esc(item.resolution_status)} · ${item.occurrences} conversations · ${item.source_count} messages · v${item.version}${item.match_score!==undefined?` · ${Math.round(item.match_score*100)}% match`:""}${item.has_new_activity?" · New activity detected":""}</p><p>${esc(item.problem_summary||"No problem summary")}</p>${item.conclusion_summary?`<p><b>Current result:</b> ${esc(item.conclusion_summary)}</p>`:`<p class="muted">No confirmed result yet.</p>`}<p class="muted">Last conversation ${esc(fmtDate(item.last_seen_at))} · Knowledge updated ${esc(fmtDate(item.updated_at))}</p></div><button class="secondary" data-kdetail="${item.id}">View Knowledge</button><div id="knowledgeDetail${item.id}" class="knowledge-detail"></div></article>`).join("") || "<div class='empty'>No knowledge in this view. Continue historical import or sync until recent topics mature.</div>";
}
function knowledgeMatches(rows){return rows.length?knowledgeRows(rows):"<div class='empty'>No related knowledge found.</div>";}
function bindKnowledge() {
  document.querySelectorAll("[data-kdetail]").forEach(button=>button.onclick=event=>work(event.target,async()=>{const id=button.dataset.kdetail,target=document.querySelector(`#knowledgeDetail${id}`);if(target.dataset.loaded){target.innerHTML="";delete target.dataset.loaded;return;}const [item,sources]=await Promise.all([api(`/api/knowledge/${id}`),api(`/api/knowledge/${id}/sources`)]);target.dataset.loaded="1";target.innerHTML=`<div class="detail-block"><h3>Problem</h3><p>${esc(item.problem_summary||"—")}</p><h3>Context</h3><p>${esc(item.context_summary||"—")}</p><h3>Result</h3><p>${esc(item.conclusion_summary||"No confirmed result")}</p>${item.open_questions.length?`<h3>Open questions</h3><ul>${item.open_questions.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:""}<h3>Version history</h3>${item.versions.map(v=>`<p><b>v${v.version}</b> · ${esc(v.change_type)} · ${esc(fmtDate(v.created_at))} · ${esc(v.model||"")}</p>`).join("")||"<p class='muted'>No versions</p>"}<h3>Related knowledge</h3>${item.relations.map(r=>`<p>${esc(r.relation_type)} · ${esc(r.related_title)} · ${Math.round(r.confidence*100)}%</p>`).join("")||"<p class='muted'>None</p>"}<h3>Original Zoom messages</h3>${sourceList(sources)}</div>`;}));
}

async function mentions() {
  const rows = await api("/api/mentions");
  content.innerHTML = `<div class="panel"><table><thead><tr><th>Source</th><th>Original message</th><th>Status</th></tr></thead><tbody>${rows.map(item=>`<tr><td>${esc(item.channel_name)}<br><span class="muted">${esc(item.sender_name)} · ${esc(fmtDate(item.sent_at))}</span></td><td class="body">${esc(item.body)}</td><td><select data-mention="${item.message_id}">${["new","in_progress","done","ignored"].map(status=>`<option ${status===item.status?"selected":""}>${status}</option>`).join("")}</select></td></tr>`).join("") || "<tr><td colspan='3' class='empty'>No mentions found</td></tr>"}</tbody></table></div>`;
  document.querySelectorAll("[data-mention]").forEach(select=>select.onchange=()=>api(`/api/mentions/${select.dataset.mention}`,{method:"PATCH",body:JSON.stringify({status:select.value})}));
}

async function settings() {
  const [identity, ai, knowledgeSettings] = await Promise.all([api("/api/settings/identity"), api("/api/settings/ai"), api("/api/settings/knowledge")]);
  const languageLabels = {chinese:"Chinese (default)",english:"English"};
  const tierLabels = {low:"GPT-5 mini — economical",mid:"GPT-5.6 Luna — fast (default)",high:"GPT-5.6 Terra — highest quality"};
  content.innerHTML = `<div class="panel"><h2>AI model</h2><p class="muted">The API key remains in the external configuration file and is never shown here. Knowledge is always stored in the original conversation language; this language preference is used for query/display behavior.</p>
    <div class="field"><label>Model tier</label><select id="modelTier">${Object.entries(ai.model_tiers).map(([tier,model])=>`<option value="${tier}" ${tier===ai.model_tier?"selected":""}>${esc(tierLabels[tier]||tier)} · ${esc(model)}</option>`).join("")}</select></div>
    <div class="field"><label>Preferred query language</label><select id="outputLanguage">${ai.output_languages.map(language=>`<option value="${language}" ${language===ai.output_language?"selected":""}>${esc(languageLabels[language]||language)}</option>`).join("")}</select></div>
    <p class="muted">Credential source: ${esc(ai.config_source)} · API ${ai.configured?"configured":"not configured"}</p><button class="primary" id="saveAI">Save AI Settings</button></div>
    <div class="panel"><h2>Knowledge maturity</h2><p class="muted">Recent topics stay in Conversation Topics. After this quiet period they are automatically archived to the Knowledge Base. Changing the period never deletes messages or knowledge.</p><div class="field"><label>Archive after</label><select id="maturityDays">${knowledgeSettings.allowed_days.map(day=>`<option value="${day}" ${day===knowledgeSettings.maturity_days?"selected":""}>${day} days${day===knowledgeSettings.default_days?" (default)":""}</option>`).join("")}</select></div><button class="primary" id="saveKnowledgeSettings">Save Knowledge Settings</button></div>
    <div class="panel"><h2>Mention identity</h2><p class="muted">Zoom member ID is the most accurate match. Display name is used as a fallback for newly synced messages.</p><div class="field"><label>Zoom member ID</label><input id="memberId" value="${esc(identity.member_id)}"></div><div class="field"><label>Zoom display name</label><input id="displayName" value="${esc(identity.display_name)}"></div><button class="primary" id="saveIdentity">Save Identity</button></div>`;
  document.querySelector("#saveAI").onclick=event=>work(event.target,async()=>{const result=await api("/api/settings/ai",{method:"PUT",body:JSON.stringify({model_tier:document.querySelector("#modelTier").value,output_language:document.querySelector("#outputLanguage").value})});show(`AI settings saved: ${result.model} · ${result.output_language}`);await settings();});
  document.querySelector("#saveKnowledgeSettings").onclick=event=>work(event.target,async()=>{const maturity_days=Number(document.querySelector("#maturityDays").value);await api("/api/settings/knowledge",{method:"PUT",body:JSON.stringify({maturity_days})});show(`Knowledge maturity set to ${maturity_days} days.`);await settings();});
  document.querySelector("#saveIdentity").onclick=event=>work(event.target,async()=>{await api("/api/settings/identity",{method:"PUT",body:JSON.stringify({member_id:document.querySelector("#memberId").value,display_name:document.querySelector("#displayName").value})});show("Identity settings saved");});
}

async function work(button, fn, progress = null) { const original=button.textContent;button.disabled=true;if(progress){button.textContent=progress.label;startProgress(progress.title,progress.detail);}try{await fn();}catch(error){show(error.message,true);}finally{if(progress)stopProgress();button.textContent=original;button.disabled=false;} }
document.querySelectorAll("nav button").forEach(button=>button.onclick=()=>render(button.dataset.view));
syncButton.onclick=event=>work(event.target,async()=>{const result=await api("/api/sync-runs",{method:"POST"});const extraction=result.topic_extraction||{};const archive=result.knowledge_archive||{};show(`Sync complete: ${result.channels} channels, ${result.messages} messages inserted or updated${result.errors.length?`, ${result.errors.length} errors`:""}. Recent topics: ${extraction.topics||0} created. Mature knowledge: ${archive.created||0} created, ${archive.updated||0} updated, ${archive.related||0} related, ${archive.conflicts||0} conflicts.`);await render();},{label:"Syncing…",title:"Syncing channels, extracting topics, and archiving knowledge",detail:"Fetching new Zoom Chat messages, extracting recent conversation topics, and moving eligible completed discussions into the Knowledge Base."});
render();
