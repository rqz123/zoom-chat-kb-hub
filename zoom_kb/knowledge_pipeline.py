from __future__ import annotations

import hashlib, json, math, re, time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Literal
from pydantic import BaseModel, Field

from .ai_topics import ExtractedTopic, TopicAIService, detect_source_language
from .config import DEFAULT_KNOWLEDGE_MATURITY_DAYS, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, KNOWLEDGE_PROMPT_VERSION
from .db import Database
from .knowledge import normalize_question
from .sync import now_iso


class SearchTranslations(BaseModel):
    english: str
    chinese: str


class ArchiveDecision(BaseModel):
    action: Literal["create", "update", "related", "conflict"]
    target_knowledge_id: int
    confidence: float = Field(ge=0, le=1)
    reason: str


class KnowledgePipeline:
    def __init__(self, db: Database, topic_service: TopicAIService | None = None):
        self.db, self.topic_service = db, topic_service or TopicAIService(db)

    def maturity_days(self) -> int:
        try: return int(self.db.get_setting("knowledge_maturity_days") or DEFAULT_KNOWLEDGE_MATURITY_DAYS)
        except ValueError: return DEFAULT_KNOWLEDGE_MATURITY_DAYS

    def cutoff(self) -> str:
        return (datetime.now(timezone.utc)-timedelta(days=self.maturity_days())).replace(microsecond=0).isoformat().replace("+00:00","Z")

    def bootstrap_status(self) -> dict[str, Any]:
        with self.db.connect() as c:
            row=c.execute("SELECT * FROM knowledge_import_runs WHERE mode='historical' ORDER BY id DESC LIMIT 1").fetchone()
            aggregate=c.execute("""SELECT MIN(started_at) first_started,MAX(finished_at) last_finished,
                SUM(processed_windows) processed,SUM(identified_topics) identified,SUM(knowledge_created) created,
                SUM(knowledge_updated) updated,SUM(knowledge_related) related,SUM(knowledge_conflicts) conflicts,
                SUM(error_count) errors FROM knowledge_import_runs WHERE mode='historical'""").fetchone()
        windows=self._eligible_historical_windows(); remaining=sum(not self._archive_succeeded(self._hash(w,detect_source_language(w['messages']),'historical')) for w in windows)
        result=dict(row) if row else {"status":"not_started","started_at":None,"finished_at":None}
        if aggregate and aggregate['first_started']:
            result.update(processed_windows=aggregate['processed'] or 0,identified_topics=aggregate['identified'] or 0,
                          knowledge_created=aggregate['created'] or 0,knowledge_updated=aggregate['updated'] or 0,
                          knowledge_related=aggregate['related'] or 0,knowledge_conflicts=aggregate['conflicts'] or 0,
                          error_count=aggregate['errors'] or 0,started_at=aggregate['first_started'],finished_at=aggregate['last_finished'])
        if remaining==0 and row:result['status']='completed'
        result.update(remaining_windows=remaining,maturity_days=self.maturity_days(),elapsed_seconds=self._elapsed(result.get("started_at"),result.get("finished_at")))
        return result

    def bootstrap_historical(self, max_windows: int=8) -> dict[str, Any]:
        settings=self.topic_service._resolved_settings()
        if not settings.configured: raise RuntimeError("OpenAI API key is not configured.")
        windows=self._eligible_historical_windows(); stamp=now_iso()
        with self.db.connect() as c:
            rid=int(c.execute("INSERT INTO knowledge_import_runs(mode,status,cutoff_at,total_windows,scanned_messages,started_at) VALUES('historical','running',?,?,?,?)",(self.cutoff(),len(windows),sum(len(w['messages']) for w in windows),stamp)).lastrowid)
        totals={k:0 for k in ('processed','skipped','identified','created','updated','related','conflicts')}; errors=[]; attempted=0
        for w in windows:
            lang=detect_source_language(w['messages']); h=self._hash(w,lang,'historical')
            if self._archive_succeeded(h): totals['skipped']+=1; continue
            if attempted>=max_windows: break
            attempted+=1; ar=self._start_run(w,h,'historical',settings.model)
            try:
                result,meta=self.topic_service._extract(w,replace(settings,output_language=lang)); stats=self._archive_result(result.topics,w,lang,None,settings.model,h)
                totals['processed']+=1; totals['identified']+=len(result.topics)
                for k in stats: totals[k]+=stats[k]
                self._finish_run(ar,'succeeded',sum(stats.values()),meta)
            except Exception as e:
                self._finish_run(ar,'failed',0,error=str(e)[:1000]); errors.append({'channel':w['channel_name'],'error':str(e)[:300]})
        status='completed' if totals['processed']+totals['skipped']>=len(windows) and not errors else 'partial'
        with self.db.connect() as c:
            c.execute("""UPDATE knowledge_import_runs SET status=?,processed_windows=?,skipped_windows=?,identified_topics=?,knowledge_created=?,knowledge_updated=?,knowledge_related=?,knowledge_conflicts=?,error_count=?,detail=?,finished_at=? WHERE id=?""",
                      (status,totals['processed'],totals['skipped'],totals['identified'],totals['created'],totals['updated'],totals['related'],totals['conflicts'],len(errors),json.dumps(errors,ensure_ascii=False),now_iso(),rid))
        return {'run_id':rid,'status':status,'total_windows':len(windows),**totals,'errors':errors}

    def archive_mature_topics(self,max_topics:int=8)->dict[str,Any]:
        settings=self.topic_service._resolved_settings()
        if not settings.configured:return {'processed':0,'archived_topics':0,'created':0,'updated':0,'related':0,'conflicts':0,'errors':['OpenAI is not configured']}
        with self.db.connect() as c: ids=[r['id'] for r in c.execute("SELECT id FROM conversation_topics WHERE superseded_by_id IS NULL AND ignored_at IS NULL AND archived_at IS NULL AND last_message_at<=? ORDER BY last_message_at LIMIT ?",(self.cutoff(),max_topics))]
        totals={k:0 for k in ('processed','archived_topics','created','updated','related','conflicts')}; errors=[]
        for tid in ids:
            w=self._topic_window(tid)
            if not w:continue
            lang=detect_source_language(w['messages']); h=self._hash(w,lang,'mature')
            if self._archive_succeeded(h):self._mark_archived(tid);totals['archived_topics']+=1;continue
            ar=self._start_run(w,h,'mature',settings.model)
            try:
                result,meta=self.topic_service._extract(w,replace(settings,output_language=lang)); stats=self._archive_result(result.topics,w,lang,tid,settings.model,h); totals['processed']+=1
                for k in stats:totals[k]+=stats[k]
                self._finish_run(ar,'succeeded',sum(stats.values()),meta);self._mark_archived(tid);totals['archived_topics']+=1
            except Exception as e:self._finish_run(ar,'failed',0,error=str(e)[:1000]);errors.append(str(e)[:300])
        return {**totals,'errors':errors}

    def recent(self,view:str='added',limit:int=30)->list[dict[str,Any]]:
        return self._rows(limit,view if view in {'added','updated','unresolved','activity'} else 'added')

    def detail(self,kid:int)->dict[str,Any]|None:
        rows=self._rows(1,kid=kid)
        if not rows:return None
        out=rows[0]
        with self.db.connect() as c:
            versions=[dict(r) for r in c.execute("SELECT * FROM knowledge_versions WHERE knowledge_id=? ORDER BY version DESC",(kid,))]
            relations=[dict(r) for r in c.execute("""SELECT r.*,CASE WHEN r.source_knowledge_id=? THEN r.target_knowledge_id ELSE r.source_knowledge_id END related_id,k.canonical_question related_title,k.source_language related_language,k.resolution_status related_status FROM knowledge_relations r JOIN knowledge_items k ON k.id=CASE WHEN r.source_knowledge_id=? THEN r.target_knowledge_id ELSE r.source_knowledge_id END WHERE r.source_knowledge_id=? OR r.target_knowledge_id=? ORDER BY r.created_at DESC""",(kid,kid,kid,kid))]
            topics=[dict(r) for r in c.execute("""SELECT t.id,t.title,t.status,t.first_message_at,t.last_message_at,c.name channel_name FROM knowledge_topic_sources s JOIN conversation_topics t ON t.id=s.topic_id JOIN channels c ON c.id=t.channel_id WHERE s.knowledge_id=? ORDER BY t.last_message_at DESC""",(kid,))]
        for v in versions:v['snapshot']=json.loads(v.pop('snapshot_json') or '{}')
        out.update(versions=versions,relations=relations,topics=topics);return out

    def search(self,query:str,limit:int=10,translate:bool=True)->list[dict[str,Any]]:
        start=time.monotonic();query=query.strip()
        if not query:return self.recent('updated',limit)
        variants=[query]+(self._translate(query) if translate else []);qvec=self._embed(query);rows=self._rows(1000)
        for r in rows:
            text=self._knowledge_text(r);lex=max(self._similarity(q,text) for q in variants);vec=self._vector(r,text);sem=self._cosine(qvec,vec);structured=self._structured(query,text)
            r.update(lexical_score=round(lex,4),semantic_score=round(sem,4),structured_score=round(structured,4),match_score=round(.55*sem+.25*lex+.20*structured,4))
        ranked=sorted((r for r in rows if r['match_score']>=.08),key=lambda r:(-r['match_score'],r['canonical_question']))[:limit]
        with self.db.connect() as c:c.execute("INSERT INTO search_runs(query_language,translated,candidate_count,result_count,duration_ms,created_at) VALUES(?,?,?,?,?,?)",(self._language(query),int(len(variants)>1),len(rows),len(ranked),int((time.monotonic()-start)*1000),now_iso()))
        return ranked

    def _archive_result(self,topics,w,lang,topic_id,model,h):
        stats={k:0 for k in ('created','updated','related','conflicts')};m={x['zoom_message_id']:x for x in w['messages']}
        for t in topics:
            sources=[m[x] for x in dict.fromkeys(t.source_message_ids) if x in m]
            if not sources:continue
            action=self._archive_topic(t,sources,lang,topic_id,model,h);stats['conflicts' if action=='conflict' else action]+=1
        return stats

    def _archive_topic(self,t,sources,lang,topic_id,model,h):
        text=self._topic_text(t);vec=self._embed(text);candidates=self._candidates(t,vec)
        exact=next((x for x in candidates if x['normalized_key']==f"{lang}:{normalize_question(t.title)}"),None)
        decision=ArchiveDecision(action='update',target_knowledge_id=exact['id'],confidence=1,reason='Exact title and language') if exact else self._decide(t,lang,candidates)
        valid={x['id'] for x in candidates}
        if decision.target_knowledge_id not in valid:decision=ArchiveDecision(action='create',target_knowledge_id=0,confidence=0,reason='No valid target')
        target=next((x for x in candidates if x['id']==decision.target_knowledge_id),None)
        if decision.action=='update' and target and target['source_language']!=lang:decision=ArchiveDecision(action='related',target_knowledge_id=target['id'],confidence=decision.confidence,reason='Cross-language records stay separate')
        if decision.action=='update':self._write_version(target['id'],t,sources,lang,topic_id,model,h,'update');self._store_vector(target['id'],text,vec);return 'updated'
        kid=self._create(t,sources,lang,topic_id,model,h);self._store_vector(kid,text,vec)
        if decision.action in {'related','conflict'} and target:self._relation(kid,target['id'],decision.action,decision.confidence,decision.reason);return decision.action
        return 'created'

    def _candidates(self,t,vec):
        text=self._topic_text(t);rows=self._rows(1000)
        for r in rows:
            other=self._knowledge_text(r);r['candidate_score']=.55*self._cosine(vec,self._vector(r,other))+.25*self._similarity(t.title+' '+t.problem_summary,other)+.20*self._structured(text,other)
        return sorted((r for r in rows if r['candidate_score']>=.35),key=lambda r:-r['candidate_score'])[:5]

    def _decide(self,t,lang,candidates):
        if not candidates:return ArchiveDecision(action='create',target_knowledge_id=0,confidence=1,reason='No candidate')
        settings=self.topic_service._resolved_settings()
        try:
            from openai import OpenAI
            kw={'api_key':settings.api_key,'timeout':60.0,'max_retries':1};
            if settings.base_url:kw['base_url']=settings.base_url
            payload={'new':{'language':lang,'title':t.title,'problem':t.problem_summary,'context':t.context_summary,'conclusions':t.conclusions},'candidates':[{k:r[k] for k in ('id','canonical_question','problem_summary','context_summary','conclusion_summary','source_language','resolution_status','candidate_score')} for r in candidates]}
            res=OpenAI(**kw).responses.parse(model=settings.model,instructions='Choose create, update, related, or conflict. Update only the same reusable problem in the same language. Different languages stay separate. Conflict means conclusions disagree. Use a supplied ID, or 0 for create.',input=json.dumps(payload,ensure_ascii=False),text_format=ArchiveDecision,store=False)
            if res.output_parsed:return res.output_parsed
        except Exception:pass
        b=candidates[0]
        if b['candidate_score']>=.86 and b['source_language']==lang:return ArchiveDecision(action='update',target_knowledge_id=b['id'],confidence=b['candidate_score'],reason='High hybrid similarity')
        if b['candidate_score']>=.55:return ArchiveDecision(action='related',target_knowledge_id=b['id'],confidence=b['candidate_score'],reason='Related, unsafe to merge')
        return ArchiveDecision(action='create',target_knowledge_id=0,confidence=1-b['candidate_score'],reason='Low similarity')

    def _create(self,t,sources,lang,topic_id,model,h):
        stamp=now_iso();base=f"{lang}:{normalize_question(t.title)}";key=base
        with self.db.connect() as c:
            n=2
            while c.execute('SELECT 1 FROM knowledge_items WHERE normalized_key=?',(key,)).fetchone():key=f'{base}:{n}';n+=1
            first,last=min(s['sent_at'] for s in sources),max(s['sent_at'] for s in sources);con='\n'.join(t.conclusions).strip();resolution=self._resolution(t)
            kid=int(c.execute("""INSERT INTO knowledge_items(canonical_question,normalized_key,answer,category,status,occurrences,first_seen_at,last_seen_at,updated_at,problem_summary,conclusion_summary,context_summary,open_questions_json,action_items_json,version,last_verified_at,source_language,resolution_status,tags_json,origin_version,has_new_activity,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(t.title.strip(),key,con,'uncategorized','active',1,first,last,stamp,t.problem_summary.strip(),con,t.context_summary.strip(),json.dumps(t.open_questions,ensure_ascii=False),json.dumps([a.model_dump() for a in t.action_items],ensure_ascii=False),1,stamp,lang,resolution,json.dumps(t.tags,ensure_ascii=False),'v0.2',0,stamp)).lastrowid)
        self._write_version(kid,t,sources,lang,topic_id,model,h,'create',False);return kid

    def _write_version(self,kid,t,sources,lang,topic_id,model,h,change,increment=True):
        stamp=now_iso();con='\n'.join(t.conclusions).strip();resolution=self._resolution(t);first,last=min(s['sent_at'] for s in sources),max(s['sent_at'] for s in sources)
        with self.db.connect() as c:
            row=c.execute('SELECT version FROM knowledge_items WHERE id=?',(kid,)).fetchone();version=int(row['version'])+1 if increment else 1
            if increment:c.execute("""UPDATE knowledge_items SET canonical_question=?,answer=?,problem_summary=?,conclusion_summary=?,context_summary=?,open_questions_json=?,action_items_json=?,tags_json=?,resolution_status=?,occurrences=occurrences+1,first_seen_at=MIN(first_seen_at,?),last_seen_at=MAX(last_seen_at,?),version=?,last_verified_at=?,updated_at=?,has_new_activity=0 WHERE id=?""",(t.title.strip(),con,t.problem_summary.strip(),con,t.context_summary.strip(),json.dumps(t.open_questions,ensure_ascii=False),json.dumps([a.model_dump() for a in t.action_items],ensure_ascii=False),json.dumps(t.tags,ensure_ascii=False),resolution,first,last,version,stamp,stamp,kid))
            snap={'title':t.title,'problem':t.problem_summary,'context':t.context_summary,'discussion':t.discussion_summary,'facts':t.confirmed_facts,'conclusions':t.conclusions,'open_questions':t.open_questions,'action_items':[a.model_dump() for a in t.action_items],'tags':t.tags,'source_language':lang,'resolution_status':resolution}
            vid=int(c.execute("INSERT INTO knowledge_versions(knowledge_id,version,snapshot_json,created_at,model,prompt_version,input_hash,source_language,resolution_status,change_type) VALUES(?,?,?,?,?,?,?,?,?,?)",(kid,version,json.dumps(snap,ensure_ascii=False),stamp,model,KNOWLEDGE_PROMPT_VERSION,h,lang,resolution,change)).lastrowid)
            c.executemany("INSERT OR IGNORE INTO knowledge_sources(knowledge_id,message_id,relation) VALUES(?,?,'archive')",[(kid,s['local_id']) for s in sources]);c.executemany('INSERT INTO knowledge_version_sources(version_id,message_id) VALUES(?,?)',[(vid,s['local_id']) for s in sources])
            if topic_id is not None:c.execute('INSERT OR IGNORE INTO knowledge_topic_sources(knowledge_id,topic_id) VALUES(?,?)',(kid,topic_id))
            self._fts(c,kid)

    def _rows(self,limit,view=None,kid=None):
        clauses=["k.origin_version='v0.2'"];p=[]
        if kid is not None:clauses.append('k.id=?');p.append(kid)
        if view=='unresolved':clauses.append("k.resolution_status IN ('unresolved','partially_resolved','waiting')")
        if view=='activity':clauses.append('k.has_new_activity=1')
        order='k.created_at DESC' if view=='added' else 'k.updated_at DESC';p.append(limit)
        with self.db.connect() as c:rows=[dict(r) for r in c.execute(f"SELECT k.*,(SELECT COUNT(*) FROM knowledge_sources s WHERE s.knowledge_id=k.id) source_count,(SELECT COUNT(*) FROM knowledge_relations x WHERE x.source_knowledge_id=k.id OR x.target_knowledge_id=k.id) relation_count FROM knowledge_items k WHERE {' AND '.join(clauses)} ORDER BY {order} LIMIT ?",p)]
        for r in rows:
            for k in ('open_questions_json','action_items_json','tags_json'):r[k.removesuffix('_json')]=json.loads(r.pop(k) or '[]')
        return rows

    def _eligible_historical_windows(self):
        windows=[w for w in self.topic_service.build_windows(None) if max(m['sent_at'] for m in w['messages'])<=self.cutoff()]
        with self.db.connect() as c:ignored={r['message_id'] for r in c.execute('SELECT ts.message_id FROM conversation_topics t JOIN topic_sources ts ON ts.topic_id=t.id WHERE t.ignored_at IS NOT NULL')}
        return [w for w in windows if not ({m['local_id'] for m in w['messages']} & ignored)]

    def _topic_window(self,tid):
        with self.db.connect() as c:rows=[dict(r) for r in c.execute("SELECT m.id local_id,m.zoom_message_id,m.channel_id,m.sender_name,m.sent_at,m.body,m.thread_id,m.reply_to_message_id,c.name channel_name FROM topic_sources ts JOIN messages m ON m.id=ts.message_id JOIN channels c ON c.id=m.channel_id WHERE ts.topic_id=? ORDER BY m.sent_at",(tid,))]
        return None if not rows else {'window_key':f'topic:{tid}','channel_id':rows[0]['channel_id'],'channel_name':rows[0]['channel_name'],'messages':rows}

    def _relation(self,a,b,kind,confidence,reason):
        a,b=sorted((a,b))
        with self.db.connect() as c:c.execute("INSERT INTO knowledge_relations(source_knowledge_id,target_knowledge_id,relation_type,confidence,reason,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(source_knowledge_id,target_knowledge_id,relation_type) DO UPDATE SET confidence=excluded.confidence,reason=excluded.reason",(a,b,kind,confidence,reason,now_iso()))

    def _embed(self,text):
        s=self.topic_service._resolved_settings()
        if not s.configured:return []
        try:
            from openai import OpenAI
            kw={'api_key':s.api_key,'timeout':60.0,'max_retries':1};
            if s.base_url:kw['base_url']=s.base_url
            return list(OpenAI(**kw).embeddings.create(model=EMBEDDING_MODEL,input=text[:24000],dimensions=EMBEDDING_DIMENSIONS,encoding_format='float').data[0].embedding)
        except Exception:return []

    def _vector(self,row,text):
        h=hashlib.sha256(text.encode()).hexdigest()
        with self.db.connect() as c:s=c.execute('SELECT * FROM knowledge_embeddings WHERE knowledge_id=?',(row['id'],)).fetchone()
        if s and s['model']==EMBEDDING_MODEL and s['content_hash']==h:return json.loads(s['vector_json'])
        v=self._embed(text);self._store_vector(row['id'],text,v);return v

    def _store_vector(self,kid,text,v):
        if not v:return
        with self.db.connect() as c:c.execute("INSERT INTO knowledge_embeddings(knowledge_id,model,dimensions,content_hash,vector_json,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(knowledge_id) DO UPDATE SET model=excluded.model,dimensions=excluded.dimensions,content_hash=excluded.content_hash,vector_json=excluded.vector_json,updated_at=excluded.updated_at",(kid,EMBEDDING_MODEL,len(v),hashlib.sha256(text.encode()).hexdigest(),json.dumps(v),now_iso()))

    def _translate(self,q):
        s=self.topic_service._resolved_settings()
        try:
            from openai import OpenAI
            kw={'api_key':s.api_key,'timeout':45.0,'max_retries':1};
            if s.base_url:kw['base_url']=s.base_url
            p=OpenAI(**kw).responses.parse(model=s.model,instructions='Translate this search concept into concise English and Simplified Chinese phrases. Do not answer it.',input=q,text_format=SearchTranslations,store=False).output_parsed
            return [p.english,p.chinese] if p else []
        except Exception:return []

    def _fts(self,c,kid):
        r=c.execute('SELECT * FROM knowledge_items WHERE id=?',(kid,)).fetchone();c.execute('DELETE FROM knowledge_fts WHERE knowledge_id=?',(kid,));c.execute('INSERT INTO knowledge_fts(knowledge_id,title,problem,context,conclusion,tags) VALUES(?,?,?,?,?,?)',(kid,r['canonical_question'],r['problem_summary'],r['context_summary'],r['conclusion_summary'],r['tags_json']))

    def _hash(self,w,lang,mode):
        s=self.topic_service._resolved_settings();x={'version':KNOWLEDGE_PROMPT_VERSION,'model':s.model,'mode':mode,'language':lang,'messages':[[m['zoom_message_id'],m['sent_at'],m['body']] for m in w['messages']]};return hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    def _archive_succeeded(self,h):
        with self.db.connect() as c:return c.execute("SELECT 1 FROM knowledge_archive_runs WHERE input_hash=? AND status='succeeded'",(h,)).fetchone() is not None
    def _start_run(self,w,h,mode,model):
        with self.db.connect() as c:
            r=c.execute('SELECT id FROM knowledge_archive_runs WHERE input_hash=?',(h,)).fetchone()
            if r:c.execute("UPDATE knowledge_archive_runs SET status='running',error=NULL,started_at=?,finished_at=NULL,model=?,prompt_version=? WHERE id=?",(now_iso(),model,KNOWLEDGE_PROMPT_VERSION,r['id']));return r['id']
            return c.execute("INSERT INTO knowledge_archive_runs(input_hash,mode,channel_id,status,message_count,started_at,model,prompt_version) VALUES(?,?,?,'running',?,?,?,?)",(h,mode,w['channel_id'],len(w['messages']),now_iso(),model,KNOWLEDGE_PROMPT_VERSION)).lastrowid
    def _finish_run(self,rid,status,count,meta=None,error=None):
        meta=meta or {}
        with self.db.connect() as c:c.execute('UPDATE knowledge_archive_runs SET status=?,knowledge_count=?,error=?,input_tokens=?,output_tokens=?,finished_at=? WHERE id=?',(status,count,error,meta.get('input_tokens'),meta.get('output_tokens'),now_iso(),rid))
    def _mark_archived(self,tid):
        with self.db.connect() as c:c.execute('UPDATE conversation_topics SET archived_at=?,updated_at=? WHERE id=?',(now_iso(),now_iso(),tid))

    @staticmethod
    def _resolution(t):
        return 'resolved' if t.status=='resolved' else 'waiting' if t.status=='waiting' else 'partially_resolved' if t.conclusions else 'unresolved'
    @staticmethod
    def _topic_text(t):return ' '.join([t.title,t.problem_summary,t.context_summary,t.discussion_summary,*t.tags,*t.conclusions])
    @staticmethod
    def _knowledge_text(r):return ' '.join(str(r.get(k) or '') for k in ('canonical_question','problem_summary','context_summary','conclusion_summary'))+' '+' '.join(r.get('tags') or [])
    @staticmethod
    def _language(x):
        c=sum('\u4e00'<=z<='\u9fff' for z in x);l=sum(z.isascii() and z.isalpha() for z in x);return 'chinese' if c>=3 and c*2>=l else 'english'
    @staticmethod
    def _cosine(a,b):
        if not a or len(a)!=len(b):return 0.0
        d=sum(x*y for x,y in zip(a,b));n=math.sqrt(sum(x*x for x in a))*math.sqrt(sum(y*y for y in b));return d/n if n else 0.0
    @staticmethod
    def _structured(a,b):
        p=r'\b(?:[A-Z]{2,}[A-Z0-9-]*|[A-Z]*\d+[A-Z0-9-]*|\d{3,})\b';x=set(re.findall(p,a,re.I));y=set(re.findall(p,b,re.I));return len(x&y)/max(1,len(x)) if x else 0.0
    @staticmethod
    def _similarity(a,b):
        a,b=a.casefold().strip(),b.casefold()
        if not a or not b:return 0.0
        if a in b:return 1.0
        w=set(re.findall(r'[a-z0-9]+',a));c=''.join(re.findall(r'[\u4e00-\u9fff]',a));t=w|{c[i:i+2] for i in range(max(0,len(c)-1))};h=set(re.findall(r'[a-z0-9]+',b))|set(re.findall(r'[\u4e00-\u9fff]{2}',b));return max(len(t&h)/max(1,len(t)),SequenceMatcher(None,a[:300],b[:1500]).ratio()*.6)
    @staticmethod
    def _elapsed(a,b):
        if not a:return 0
        x=datetime.fromisoformat(a.replace('Z','+00:00'));y=datetime.fromisoformat(b.replace('Z','+00:00')) if b else datetime.now(timezone.utc);return max(0,int((y-x).total_seconds()))
