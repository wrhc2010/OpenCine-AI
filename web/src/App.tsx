import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  Activity, ArrowRight, Check, CircleAlert, Film, Gauge, GitBranch, Inbox, KeyRound,
  LoaderCircle, LogOut, Play, RotateCcw, Save, Settings2, ShieldCheck, Sparkles, WalletCards,
} from 'lucide-react';
import './styles.css';

type AuthState = { enabled: boolean; initialized: boolean; authenticated: boolean; username?: string | null };
type ClarificationOption = { id: string; label: string; value: string; explanation: string };
type ClarificationTurn = { id: string; question: string; confirmed: boolean; options?: ClarificationOption[]; skipped?: boolean; answer?: string | null };
type Brief = {
  request: string; title: string; budget_usd: number; duration_seconds: number; shot_duration_seconds: number;
  parallelism_mode: string; parallelism?: number | null; resolution_mode: string; resolution_width?: number | null;
  resolution_height?: number | null; acceptance_mode: string; acceptance_custom?: string | null;
};
type Criterion = { id: string; category: string; statement: string };
type Shot = { id: string; sequence: number; title: string; description: string; acceptance_criteria: Criterion[]; prompt_bundle?: { positive: string; version: number } };
type Attempt = { shot_id: string; number: number; status: string; judge_result?: { verdict: string; criterion_results: { criterion_id: string; verdict: string; failure_code?: string; reason?: string; skipped?: boolean }[] } };
type Project = { id: string; name: string; status: string; total_cost_usd: number; brief: Brief; clarification_turns: ClarificationTurn[]; plans: { shots: Shot[]; resolved_settings?: Record<string, unknown> }[]; attempts: Attempt[]; artifacts: { uri: string }[] };
type EventRecord = { id: number; event_type: string; payload: Record<string, unknown>; created_at: string };
type SettingsResponse = { settings: Record<string, string | number | boolean>; requires_restart: string[]; llm_configured?: boolean; vlm_configured?: boolean };
type Answer = string | { answer?: string; skip?: boolean };

const briefPayload = {
  title: '新的视频项目',
  request: '一位主角在雨夜城市找到一封遗失的信，并在黎明前将它归还。包含旁白、音乐和清晰字幕。',
  duration_seconds: 30,
  shot_duration_seconds: 15,
  max_shots: 10,
  budget_usd: 75,
  language: 'zh-CN',
  audio_required: true,
  style: '电影感写实',
};

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, credentials: 'include', headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(String(body.detail || `请求失败（${response.status}）`));
    (error as Error & { status?: number }).status = response.status;
    throw error;
  }
  return response.json() as Promise<T>;
}

export function App() {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [project, setProject] = useState<Project | null>(null);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [selected, setSelected] = useState(0);
  const [running, setRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState('正在连接导演运行时…');
  const [error, setError] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<string, Answer>>({});
  const [activeNav, setActiveNav] = useState<'control' | 'projects' | 'review' | 'settings'>('control');
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [settingsDraft, setSettingsDraft] = useState<Record<string, string | number | boolean>>({});
  const [setupForm, setSetupForm] = useState({ username: '', password: '', confirm: '' });
  const [loginForm, setLoginForm] = useState({ username: '', password: '' });

  const refresh = useCallback(async (projectId: string) => {
    const [next, nextEvents] = await Promise.all([apiFetch<Project>(`/v1/projects/${projectId}`), apiFetch<EventRecord[]>(`/v1/projects/${projectId}/events`)]);
    setProject(next); setEvents(nextEvents);
    const count = next.plans.length ? next.plans[next.plans.length - 1].shots.length : 1;
    setSelected((current) => Math.min(current, Math.max(0, count - 1)));
    setNotice(next.clarification_turns.filter((turn) => !turn.confirmed).length ? '还有需求信息待确认' : statusLabel(next.status));
    setError(null);
  }, []);

  const bootstrapAuth = useCallback(async () => {
    try {
      const status = await apiFetch<{ enabled: boolean; initialized: boolean; username?: string | null }>('/v1/auth/status');
      if (!status.enabled) setAuth({ ...status, authenticated: true });
      else if (!status.initialized) setAuth({ ...status, authenticated: false });
      else {
        try { const me = await apiFetch<{ username: string }>('/v1/auth/me'); setAuth({ ...status, authenticated: true, username: me.username }); }
        catch { setAuth({ ...status, authenticated: false }); }
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : '认证服务不可用'); }
    finally { setAuthLoading(false); }
  }, []);

  useEffect(() => { void bootstrapAuth(); }, [bootstrapAuth]);

  useEffect(() => {
    if (!auth?.authenticated) { setLoading(false); return; }
    let cancelled = false;
    const initialize = async () => {
      try {
        let projectId = window.localStorage.getItem('director-project-id');
        let next: Project;
        if (projectId) {
          try { next = await apiFetch<Project>(`/v1/projects/${projectId}`); }
          catch { next = await apiFetch<Project>('/v1/projects', { method: 'POST', body: JSON.stringify(briefPayload) }); projectId = next.id; }
        } else { next = await apiFetch<Project>('/v1/projects', { method: 'POST', body: JSON.stringify(briefPayload) }); projectId = next.id; }
        const runtime = await apiFetch<SettingsResponse>('/v1/settings');
        setSettings(runtime); setSettingsDraft(runtime.settings);
        if (!runtime.llm_configured && next.clarification_turns.some((turn) => !turn.confirmed)) {
          const skipAnswers = Object.fromEntries(next.clarification_turns.filter((turn) => !turn.confirmed).map((turn) => [turn.id, { skip: true }]));
          next = await apiFetch<Project>(`/v1/projects/${next.id}/clarifications`, { method: 'POST', body: JSON.stringify(skipAnswers) });
          setNotice('尚未配置 LLM，已使用确定性规则跳过澄清阶段。');
        }
        if (cancelled) return;
        window.localStorage.setItem('director-project-id', projectId);
        await refresh(next.id);
      } catch (cause) { if (!cancelled) { setError(cause instanceof Error ? cause.message : '导演 API 当前不可用'); setNotice('运行时离线'); } }
      finally { if (!cancelled) setLoading(false); }
    };
    void initialize();
    return () => { cancelled = true; };
  }, [auth?.authenticated, refresh]);

  useEffect(() => {
    if (!project || !auth?.authenticated) return;
    const source = new EventSource(`/v1/projects/${project.id}/events/stream?follow=true`);
    source.onopen = () => setNotice('实时事件流已连接');
    source.onmessage = (message) => { try { const event = JSON.parse(message.data) as EventRecord; setEvents((current) => current.some((item) => item.id === event.id) ? current : current.concat(event)); } catch { /* REST refresh remains authoritative. */ } };
    source.onerror = () => setNotice('实时事件流正在重连…');
    const timer = window.setInterval(() => { void refresh(project.id); }, 5000);
    return () => { source.close(); window.clearInterval(timer); };
  }, [project?.id, auth?.authenticated, refresh]);

  const shots = project?.plans.length ? project.plans[project.plans.length - 1].shots : [];
  const resolved = project?.plans.length ? project.plans[project.plans.length - 1].resolved_settings || {} : {};
  const active = shots[selected] || shots[0];
  const latest = useMemo(() => { const map = new Map<string, Attempt>(); for (const attempt of project?.attempts || []) { const current = map.get(attempt.shot_id); if (!current || attempt.number > current.number) map.set(attempt.shot_id, attempt); } return map; }, [project?.attempts]);
  const unresolved = project?.clarification_turns.filter((turn) => !turn.confirmed) || [];
  const passCount = shots.filter((shot) => latest.get(shot.id)?.judge_result?.verdict === 'PASS').length;
  const criteriaCount = shots.reduce((sum, shot) => sum + shot.acceptance_criteria.length, 0);
  const passedCriteria = shots.reduce((sum, shot) => sum + (latest.get(shot.id)?.judge_result?.criterion_results || []).filter((item) => item.verdict === 'PASS').length, 0);

  const submitAuth = async (mode: 'setup' | 'login') => {
    setRunning(true); setError(null);
    try {
      if (mode === 'setup') {
        if (setupForm.password !== setupForm.confirm) throw new Error('两次密码输入不一致');
        await apiFetch('/v1/auth/setup', { method: 'POST', body: JSON.stringify({ username: setupForm.username, password: setupForm.password }) });
        await apiFetch('/v1/auth/login', { method: 'POST', body: JSON.stringify({ username: setupForm.username, password: setupForm.password }) });
      } else await apiFetch('/v1/auth/login', { method: 'POST', body: JSON.stringify(loginForm) });
      await bootstrapAuth();
    } catch (cause) { setError(cause instanceof Error ? cause.message : '认证失败'); }
    finally { setRunning(false); }
  };

  const submitAnswers = async () => {
    if (!project) return;
    setRunning(true);
    try { await apiFetch<Project>(`/v1/projects/${project.id}/clarifications`, { method: 'POST', body: JSON.stringify(answers) }); await refresh(project.id); setNotice('澄清信息已确认。'); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '澄清信息保存失败'); }
    finally { setRunning(false); }
  };

  const saveProjectSettings = async () => {
    if (!project) return;
    setRunning(true);
    try { await apiFetch(`/v1/projects/${project.id}/settings`, { method: 'PATCH', body: JSON.stringify(project.brief) }); await refresh(project.id); setNotice('项目参数已保存，计划需要重新审批。'); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '项目参数保存失败'); }
    finally { setRunning(false); }
  };

  const loadSettings = async () => {
    try { const response = await apiFetch<SettingsResponse>('/v1/settings'); setSettings(response); setSettingsDraft(response.settings); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '高级设置读取失败'); }
  };

  const saveSettings = async () => {
    setRunning(true);
    try { const response = await apiFetch<SettingsResponse>('/v1/settings', { method: 'PATCH', body: JSON.stringify(settingsDraft) }); setSettings(response); setSettingsDraft(response.settings); setNotice('高级设置已保存。连接类配置将在新任务或重启后完全生效。'); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '高级设置保存失败'); }
    finally { setRunning(false); }
  };

  const logout = async () => { await apiFetch('/v1/auth/logout', { method: 'POST' }).catch(() => undefined); setAuth((current) => current ? { ...current, authenticated: false } : current); };
  const runQualityLoop = async () => { if (!project) return; setRunning(true); setNotice('Agent 正在执行质量闭环…'); try { let next = project; if (!next.plans.length) next = await apiFetch<Project>(`/v1/projects/${next.id}/plan`, { method: 'POST' }); next = await apiFetch<Project>(`/v1/projects/${next.id}/run`, { method: 'POST', body: JSON.stringify({ approve_plan: true, actor: auth?.username || 'operator' }) }); await refresh(next.id); } catch (cause) { setError(cause instanceof Error ? cause.message : '质量闭环执行失败'); await refresh(project.id).catch(() => undefined); } finally { setRunning(false); } };
  const retryShot = async () => { if (!project || !active) return; setRunning(true); try { await apiFetch(`/v1/projects/${project.id}/shots/${active.id}/retry`, { method: 'POST', body: JSON.stringify({ actor: auth?.username || 'operator' }) }); await refresh(project.id); setNotice(`镜头 ${active.sequence} 已完成重试。`); } catch (cause) { setError(cause instanceof Error ? cause.message : '当前无法重试镜头'); } finally { setRunning(false); } };
  const deliver = async () => { if (!project) return; setRunning(true); try { await apiFetch(`/v1/projects/${project.id}/deliver`, { method: 'POST', body: JSON.stringify({ actor: auth?.username || 'operator' }) }); await refresh(project.id); setNotice('交付已批准，最终视频已就绪。'); } catch (cause) { setError(cause instanceof Error ? cause.message : '交付审批失败'); } finally { setRunning(false); } };
  const navigate = (section: 'control' | 'projects' | 'review' | 'settings') => {
    setActiveNav(section);
    if (section === 'settings') { void loadSettings(); return; }
    const target = section === 'control' ? 'control-room' : section === 'projects' ? 'shot-queue' : unresolved.length ? 'clarification-gate' : 'review-gate';
    window.requestAnimationFrame(() => document.getElementById(target)?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  };

  if (authLoading || loading) return <div className="loading-screen" role="status"><LoaderCircle size={20} className="spin" aria-hidden="true" />正在读取导演状态</div>;
  if (auth && auth.enabled && !auth.authenticated) { const setup = !auth.initialized; const setAuthForm = (value: { username: string; password: string; confirm?: string }) => setup ? setSetupForm({ username: value.username, password: value.password, confirm: value.confirm || '' }) : setLoginForm({ username: value.username, password: value.password }); return <AuthPanel setup={setup} form={setup ? setupForm : loginForm} setForm={setAuthForm} onSubmit={() => void submitAuth(setup ? 'setup' : 'login')} error={error} running={running} />; }

  return <><a className="skip-link" href="#control-room">跳转到主要内容</a><div className="app-shell"><aside className="sidebar"><div className="brand"><img className="brand-logo" src="/opencine-ai-logo.png" alt="OpenCine-AI" /><div><strong>OpenCine-AI</strong><span>AI 视频制作系统</span></div></div><nav aria-label="主导航"><NavButton active={activeNav === 'control'} icon={<Activity size={16} />} label="控制台" onClick={() => navigate('control')} /><NavButton active={activeNav === 'projects'} icon={<GitBranch size={16} />} label="项目" onClick={() => navigate('projects')} /><NavButton active={activeNav === 'review'} icon={<Inbox size={16} />} label="待审核" count={unresolved.length + (project?.status === 'awaiting_human' ? 1 : 0)} onClick={() => navigate('review')} /><NavButton active={activeNav === 'settings'} icon={<Settings2 size={16} />} label="高级设置" onClick={() => navigate('settings')} /></nav><div className="sidebar-foot"><div className="provider"><span className="pulse" aria-hidden="true" />运行时已连接</div><small>Provider 适配器 · Mock / Fal 风格 / ComfyUI</small><button className="quiet sidebar-logout" onClick={() => void logout()}><LogOut size={14} />退出登录</button></div></aside><main className="main" id="control-room" tabIndex={-1}><header className="topbar"><div><p className="eyebrow">项目 / {(project?.name || 'OPENCINE-AI').toUpperCase()}</p><h1>{activeNav === 'settings' ? '高级设置' : activeNav === 'projects' ? '项目与镜头' : activeNav === 'review' ? '待审核事项' : '制作控制台'}</h1></div><div className="top-actions"><span className="status-chip"><span className="dot" aria-hidden="true" />{statusLabel(project?.status)}</span><button className="icon-button" aria-label="刷新项目状态" title="刷新项目状态" onClick={() => project && refresh(project.id)}><Activity size={18} /></button><div className="avatar" aria-label={`当前用户 ${auth?.username || '操作员'}`}>{(auth?.username || 'OP').slice(0, 2).toUpperCase()}</div></div></header>{activeNav === 'settings' ? <SettingsPanel settings={settings} draft={settingsDraft} setDraft={setSettingsDraft} onSave={() => void saveSettings()} running={running} /> : <><section className="phase-bar" aria-label="制作阶段">{phases.map((name, index) => <div key={name} className={`phase ${index < phaseFor(project?.status) ? 'done ' : ''}${index === phaseFor(project?.status) ? 'current' : ''}`}><span aria-hidden="true">{index < phaseFor(project?.status) ? <Check size={13} /> : index + 1}</span>{name}{index < phases.length - 1 && <ArrowRight size={13} />}</div>)}</section><section className={`notice ${error ? 'notice-error' : ''}`} role={error ? 'alert' : 'status'}><Sparkles size={16} /><span>{error || notice}</span><button onClick={() => project && refresh(project.id)}><RotateCcw size={15} />刷新状态</button></section>{project && <ProjectSettings project={project} setProject={setProject} resolved={resolved} onSave={() => void saveProjectSettings()} running={running} />}{unresolved.length > 0 && <ClarificationPanel turns={unresolved} answers={answers} setAnswers={setAnswers} onSubmit={() => void submitAnswers()} running={running} />}<section className="metrics" aria-label="项目指标"><Metric icon={<Film size={17} />} label="镜头" value={shots.length ? `${passCount} / ${shots.length}` : '—'} detail={shots.length ? '已通过' : '尚未规划'} /><Metric icon={<ShieldCheck size={17} />} label="验收" value={criteriaCount ? `${passedCriteria} / ${criteriaCount}` : '—'} detail="项通过" /><Metric icon={<WalletCards size={17} />} label="费用" value={`$${(project?.total_cost_usd || 0).toFixed(2)}`} detail={`预算 $${(project?.brief.budget_usd || 0).toFixed(0)}`} /><Metric icon={<Gauge size={17} />} label="生成尝试" value={String(project?.attempts.length || 0)} detail="条溯源记录" /></section><div className="work-grid"><section className="panel shots-panel" id="shot-queue"><div className="panel-head"><div><p className="eyebrow">执行队列</p><h2>镜头队列</h2></div><button className="primary" onClick={() => void runQualityLoop()} disabled={running || unresolved.length > 0 || project?.status === 'delivered'}><Play size={15} />{running ? '执行中…' : shots.length ? '运行质量闭环' : '生成计划'}</button></div>{shots.length ? <div className="shot-list">{shots.map((shot, index) => { const verdict = latest.get(shot.id)?.judge_result?.verdict; return <button key={shot.id} className={`shot-row ${selected === index ? 'selected' : ''}`} aria-pressed={selected === index} onClick={() => setSelected(index)}><span className="shot-number">{String(shot.sequence).padStart(2, '0')}</span><span className="shot-copy"><strong>{shot.title}</strong><small>{shot.description}</small></span><span className={`result ${verdict === 'PASS' ? 'pass' : verdict === 'FAIL' ? 'fail' : 'queued'}`}>{verdict === 'PASS' ? <Check size={14} /> : verdict === 'FAIL' ? <CircleAlert size={14} /> : <span />}{verdictLabel(verdict || 'QUEUED')}</span><ArrowRight size={15} className="row-arrow" /></button>; })}</div> : <div className="empty-state"><CircleAlert size={18} />确认澄清信息后，系统会创建镜头计划。</div>}</section><ShotDetail active={active} latest={latest} onRetry={() => void retryShot()} running={running} /></div><section className="bottom-grid"><div className="panel timeline-panel"><div className="panel-head"><div><p className="eyebrow">审计轨迹</p><h2>Agent 决策</h2></div><button className="quiet" onClick={() => project && refresh(project.id)}>刷新事件流 <ArrowRight size={14} /></button></div><div className="events">{events.slice(-8).reverse().map((event) => <Event key={event.id} event={event} />)}{events.length === 0 && <div className="empty-state">暂时没有事件记录。</div>}</div></div><div className="panel gate-panel" id="review-gate"><p className="eyebrow">下一个人工门禁</p><h2>{project?.status === 'awaiting_human' ? '审批最终组装' : project?.status === 'delivered' ? '交付已完成' : '当前无需人工处理'}</h2><p>{project?.artifacts.length ? project.artifacts[project.artifacts.length - 1].uri : '所有镜头通过后，组装产物会显示在这里。'}</p><button className="primary wide" onClick={() => void deliver()} disabled={running || project?.status !== 'awaiting_human' || !project?.artifacts.length}><ShieldCheck size={15} />批准交付</button></div></section></>}</main></div></>;
}

function AuthPanel({ setup, form, setForm, onSubmit, error, running }: { setup: boolean; form: { username: string; password: string; confirm?: string }; setForm: (value: { username: string; password: string; confirm?: string }) => void; onSubmit: () => void; error: string | null; running: boolean }) { return <main className="auth-screen"><section className="auth-panel"><div className="brand auth-brand"><img className="brand-logo" src="/opencine-ai-logo.png" alt="OpenCine-AI" /><div><strong>OpenCine-AI</strong><span>AI 视频制作系统</span></div></div><p className="eyebrow">{setup ? '首次使用' : '管理员登录'}</p><h1>{setup ? '设置管理员账号' : '登录制作控制台'}</h1><p className="auth-copy">{setup ? '设置一个仅保存在当前部署中的管理员账号。系统不会创建固定默认密码。' : '使用已初始化的管理员账号继续。'}</p>{error && <div className="notice notice-error" role="alert"><CircleAlert size={16} />{error}</div>}<label>用户名<input value={form.username} autoComplete="username" onChange={(event) => setForm({ ...form, username: event.target.value })} /></label><label>密码<input type="password" value={form.password} autoComplete={setup ? 'new-password' : 'current-password'} onChange={(event) => setForm({ ...form, password: event.target.value })} /></label>{setup && <label>确认密码<input type="password" value={form.confirm || ''} autoComplete="new-password" onChange={(event) => setForm({ ...form, confirm: event.target.value })} /></label>}<button className="primary wide" onClick={onSubmit} disabled={running || !form.username.trim() || form.password.length < 8}><KeyRound size={16} />{running ? '处理中…' : setup ? '完成初始化' : '登录'}</button></section></main>; }
function NavButton({ active, icon, label, count, onClick }: { active: boolean; icon: ReactNode; label: string; count?: number; onClick: () => void }) { return <button className={`nav-item ${active ? 'active' : ''}`} aria-current={active ? 'page' : undefined} onClick={onClick}>{icon}{label}{count ? <b>{count}</b> : null}</button>; }
function Metric({ icon, label, value, detail }: { icon: ReactNode; label: string; value: string; detail: string }) { return <div className="metric"><span className="metric-icon">{icon}</span><div><small>{label}</small><strong>{value}</strong><em>{detail}</em></div></div>; }
function ProjectSettings({ project, setProject, resolved, onSave, running }: { project: Project; setProject: (value: Project) => void; resolved: Record<string, unknown>; onSave: () => void; running: boolean }) { const update = (key: keyof Brief, value: string | number | null) => setProject({ ...project, brief: { ...project.brief, [key]: value } }); return <section className="panel config-panel"><div className="panel-head"><div><p className="eyebrow">项目配置</p><h2>生成参数</h2></div><button className="primary" onClick={onSave} disabled={running}><Save size={15} />保存参数</button></div><div className="config-grid"><label>视频总时长（秒）<input type="number" min="1" value={project.brief.duration_seconds} onChange={(event) => update('duration_seconds', Number(event.target.value))} /></label><label>单个镜头时长（秒）<input type="number" min="1" value={project.brief.shot_duration_seconds} onChange={(event) => update('shot_duration_seconds', Number(event.target.value))} /></label><label>并发模式<select value={project.brief.parallelism_mode} onChange={(event) => update('parallelism_mode', event.target.value)}><option value="auto">自动</option><option value="preset">预设</option><option value="custom">自定义</option></select></label><label>并发数量<input type="number" min="1" value={project.brief.parallelism || ''} placeholder="由计划解析" onChange={(event) => update('parallelism', event.target.value ? Number(event.target.value) : null)} /></label><label>分辨率模式<select value={project.brief.resolution_mode} onChange={(event) => update('resolution_mode', event.target.value)}><option value="auto">自动（1080p）</option><option value="preset">预设</option><option value="custom">自定义</option></select></label><label>宽 × 高<input value={project.brief.resolution_width && project.brief.resolution_height ? `${project.brief.resolution_width} × ${project.brief.resolution_height}` : ''} placeholder="例如 1920 × 1080" onChange={(event) => { const [width, height] = event.target.value.split(/[x× ]+/).filter(Boolean).map(Number); if (width && height) setProject({ ...project, brief: { ...project.brief, resolution_width: width, resolution_height: height } }); }} /></label><label>AI 验收标准<select value={project.brief.acceptance_mode} onChange={(event) => update('acceptance_mode', event.target.value)}><option value="auto">自动</option><option value="low">低严格</option><option value="standard">标准</option><option value="strict">严格</option><option value="custom">自定义</option><option value="none">无语义验收</option></select></label>{project.brief.acceptance_mode === 'custom' && <label>自定义验收说明<input value={project.brief.acceptance_custom || ''} onChange={(event) => update('acceptance_custom', event.target.value)} /></label>}</div><div className="resolved-line">当前计划生效值：并发 {String(resolved.parallelism || '待解析')} · 分辨率 {resolved.resolution_width ? `${resolved.resolution_width} × ${resolved.resolution_height}` : '待解析'} · 验收 {acceptanceLabel(String(resolved.acceptance_mode || project.brief.acceptance_mode))}</div></section>; }
function ClarificationPanel({ turns, answers, setAnswers, onSubmit, running }: { turns: ClarificationTurn[]; answers: Record<string, Answer>; setAnswers: (value: Record<string, Answer>) => void; onSubmit: () => void; running: boolean }) { return <section className="panel clarify-panel" id="clarification-gate"><div className="panel-head"><div><p className="eyebrow">需求澄清</p><h2>请补充缺失的创作信息</h2></div><span className="gate-count">{turns.length} 项待确认</span></div><div className="clarify-list">{turns.map((turn) => { const current = answers[turn.id]; const text = typeof current === 'string' ? current : current?.answer || ''; return <div className="clarify-row" key={turn.id}><strong>{turn.question}</strong><div className="option-list">{(turn.options || []).map((option) => <label className="option" key={option.id}><input type="radio" name={turn.id} checked={text === option.value} onChange={() => setAnswers({ ...answers, [turn.id]: option.value })} /><span><b>{option.label}</b><small>{option.explanation}</small></span></label>)}</div><input aria-label={`${turn.question} 自定义回答`} value={text} placeholder="也可以填写自定义回答" onChange={(event) => setAnswers({ ...answers, [turn.id]: event.target.value })} /><label className="skip-check"><input type="checkbox" checked={typeof current !== 'string' && Boolean(current?.skip)} onChange={(event) => setAnswers({ ...answers, [turn.id]: event.target.checked ? { skip: true } : '' })} />跳过这一项</label></div>; })}</div><button className="primary clarify-submit" onClick={onSubmit} disabled={running || turns.some((turn) => { const answer = answers[turn.id]; return !answer || (typeof answer === 'string' && !answer.trim()); })}><Check size={15} />确认答案</button></section>; }
function ShotDetail({ active, latest, onRetry, running }: { active?: Shot; latest: Map<string, Attempt>; onRetry: () => void; running: boolean }) { if (!active) return <section className="panel detail-panel"><div className="empty-state">暂未选择镜头。</div></section>; const attempt = latest.get(active.id); return <section className="panel detail-panel"><div className="panel-head"><div><p className="eyebrow">镜头 {String(active.sequence).padStart(2, '0')} / 验收标准</p><h2>{active.title}</h2></div><span className="score">{verdictLabel(attempt?.judge_result?.verdict || 'PENDING')}<small>{attempt?.number ? ` · 第 ${attempt.number} 次尝试` : ''}</small></span></div><p className="description">{active.description}</p><div className="criteria">{active.acceptance_criteria.map((criterion) => { const result = attempt?.judge_result?.criterion_results.find((item) => item.criterion_id === criterion.id); const passed = result?.verdict === 'PASS'; const skipped = result?.skipped; return <div className="criterion" key={criterion.id}><span className={`criterion-icon ${passed ? 'passed' : result?.verdict === 'FAIL' ? 'failed' : ''}`}>{passed ? <Check size={13} /> : <CircleAlert size={13} />}</span><div><strong>{criterionLabel(criterion.category)}</strong><p>{criterion.statement}</p>{result?.failure_code && <small className="failure-detail">{failureCodeLabel(result.failure_code)} · {result.reason || '需要重新处理'}</small>}</div><span className={`criterion-state ${passed ? 'passed' : result?.verdict === 'FAIL' ? 'failed' : 'pending'}`}>{skipped ? '已跳过' : verdictLabel(result?.verdict || 'PENDING')}</span></div>; })}</div><div className="prompt-block"><div className="prompt-head"><span>提示词包 v{active.prompt_bundle?.version || 1}</span><button onClick={onRetry} disabled={running}><RotateCcw size={14} />重试镜头</button></div><code>{active.prompt_bundle?.positive || '计划获批后将自动生成提示词。'}</code></div></section>; }
function SettingsPanel({ settings, draft, setDraft, onSave, running }: { settings: SettingsResponse | null; draft: Record<string, string | number | boolean>; setDraft: (value: Record<string, string | number | boolean>) => void; onSave: () => void; running: boolean }) { const bools = ['DIRECTOR_AUTH_ENABLED', 'DIRECTOR_HOST_CHECK_ENABLED', 'DIRECTOR_CORS_ENABLED', 'DIRECTOR_RATE_LIMIT_ENABLED', 'DIRECTOR_CONTENT_SAFETY_ENABLED', 'DIRECTOR_PROVIDER_SAFETY_ENABLED']; const strings = ['DIRECTOR_DATABASE_URL', 'REDIS_URL', 'OBJECT_STORAGE_ENDPOINT']; return <section className="settings-page"><div className="risk-alert"><CircleAlert size={18} /><div><strong>高级设置会影响安全、费用和连接</strong><p>Host/CORS/限流和内容安全开关默认关闭；修改数据库、Provider 凭证或连接地址后，请按提示重启或创建新任务。</p></div></div><div className="panel"><div className="panel-head"><div><p className="eyebrow">运行时设置</p><h2>全局默认值</h2></div><button className="primary" onClick={onSave} disabled={running || !settings}><Save size={15} />保存设置</button></div><div className="settings-grid">{bools.map((key) => <label className="toggle-row" key={key}><span>{settingLabel(key)}</span><input type="checkbox" checked={Boolean(draft[key])} onChange={(event) => setDraft({ ...draft, [key]: event.target.checked })} /></label>)}<label>默认并发<input type="number" min="1" value={String(draft.DIRECTOR_PARALLELISM ?? '')} onChange={(event) => setDraft({ ...draft, DIRECTOR_PARALLELISM: Number(event.target.value) })} /></label><label>项目预算（美元）<input type="number" min="0" step="0.01" value={String(draft.DIRECTOR_PROJECT_BUDGET_USD ?? '')} onChange={(event) => setDraft({ ...draft, DIRECTOR_PROJECT_BUDGET_USD: Number(event.target.value) })} /></label><label>视频 Provider<input value={String(draft.VIDEO_PROVIDER ?? '')} onChange={(event) => setDraft({ ...draft, VIDEO_PROVIDER: event.target.value })} /></label><label>视频模型<input value={String(draft.VIDEO_MODEL ?? '')} onChange={(event) => setDraft({ ...draft, VIDEO_MODEL: event.target.value })} /></label>{strings.map((key) => <label key={key}>{settingLabel(key)}<input value={String(draft[key] || '')} onChange={(event) => setDraft({ ...draft, [key]: event.target.value })} /></label>)}{['OPENAI_API_KEY', 'FAL_API_KEY', 'REPLICATE_API_TOKEN'].map((key) => <label key={key}>{key}<input type="password" placeholder={String(draft[key] || '未配置')} onChange={(event) => setDraft({ ...draft, [key]: event.target.value })} /></label>)}</div></div><p className="settings-footnote">连接类配置：{settings?.requires_restart.join('、')}</p></section>; }
function Event({ event }: { event: EventRecord }) { const warning = event.event_type.includes('failed') || event.event_type.includes('repair') || event.event_type.includes('hard_stop'); return <div className="event"><span className={`event-icon ${warning ? 'warn' : ''}`}>{event.event_type.includes('judge') ? <ShieldCheck size={14} /> : warning ? <CircleAlert size={14} /> : <Sparkles size={14} />}</span><div><strong>{eventLabel(event.event_type)}</strong><p>{eventSummary(event)}</p></div><time>{formatTime(event.created_at)}</time></div>; }
function eventLabel(type: string): string { return ({ 'project.created': '项目已创建', 'clarification.answered': '需求澄清已提交', 'plan.created': '计划已生成', 'plan.approved': '计划已审批', 'generation.started': '开始生成', 'budget.hard_stop': '已触发预算硬停止', 'continuity.failed': '连续性检查失败', 'scheduler.wave.started': '执行批次已开始', 'scheduler.wave.completed': '执行批次已完成', 'shot.provider_failed': 'Provider 生成失败', 'shot.generated': '镜头已生成', 'shot.judged': '镜头验收完成', 'shot.repair_planned': '已规划镜头修复', 'assembly.ready': '组装产物已就绪', 'project.delivered': '项目已交付' } as Record<string, string>)[type] || type; }
function eventSummary(event: EventRecord): string { const payload = event.payload; if (payload.verdict) return `验收结果：${verdictLabel(String(payload.verdict))}`; if (payload.cost_usd !== undefined) return `费用：$${Number(payload.cost_usd).toFixed(2)}`; if (payload.error) return `错误：${String(payload.error)}`; if (payload.reason) return `原因：${String(payload.reason)}`; return '项目状态已保存'; }
function statusLabel(status?: string): string { return ({ clarifying: '等待需求澄清', awaiting_plan_approval: '等待计划审批', planned: '计划已就绪', generating: '正在生成', judging: '正在验收', repairing: '正在修复', awaiting_human: '等待人工审批', assembling: '正在组装', delivered: '已交付', failed: '失败', cancelled: '已取消' } as Record<string, string>)[status || ''] || '运行时离线'; }
function verdictLabel(verdict: string): string { return ({ PASS: '通过', FAIL: '失败', QUEUED: '排队中', PENDING: '待验收' } as Record<string, string>)[verdict] || verdict; }
function criterionLabel(category: string): string { return ({ subject: '主体', action: '动作', camera: '镜头', continuity: '连续性', style: '风格', technical: '技术', audio: '音频', safety: '安全' } as Record<string, string>)[category] || category; }
function failureCodeLabel(code: string): string { return ({ low_confidence: '置信度过低', missing_evidence: '缺少验收证据', judge_error: 'Judge 返回错误', provider_error: 'Provider 调用失败' } as Record<string, string>)[code] || code; }
function acceptanceLabel(value: string): string { return ({ auto: '自动', low: '低严格', standard: '标准', strict: '严格', custom: '自定义', none: '无语义验收' } as Record<string, string>)[value] || value; }
function settingLabel(value: string): string { return ({ DIRECTOR_AUTH_ENABLED: 'WebUI 登录', DIRECTOR_HOST_CHECK_ENABLED: 'Host 限制', DIRECTOR_CORS_ENABLED: 'CORS 限制', DIRECTOR_RATE_LIMIT_ENABLED: '请求限流', DIRECTOR_CONTENT_SAFETY_ENABLED: '内容安全审核', DIRECTOR_PROVIDER_SAFETY_ENABLED: 'Provider 安全策略', DIRECTOR_DATABASE_URL: '数据库连接', REDIS_URL: 'Redis 连接', OBJECT_STORAGE_ENDPOINT: '对象存储连接' } as Record<string, string>)[value] || value; }
function formatTime(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }); }
function phaseFor(status?: string): number { if (status === 'clarifying') return 0; if (status === 'awaiting_plan_approval') return 1; if (status === 'planned' || status === 'generating') return 2; if (status === 'judging') return 3; if (status === 'repairing') return 4; return 5; }
const phases = ['需求', '计划', '生成', '验收', '修复', '交付'];
