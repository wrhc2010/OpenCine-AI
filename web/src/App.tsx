import { useCallback, useEffect, useState, type ReactNode } from 'react';
import {
  Activity, ArrowLeft, ArrowRight, Check, CircleAlert, Download, ExternalLink,
  Film, FolderOpen, KeyRound, LoaderCircle, LogOut, Moon, Play, Plus,
  RefreshCw, RotateCcw, Save, Settings2, ShieldCheck, Sun, Trash2,
} from 'lucide-react';
import './styles.css';

type AuthState = { enabled: boolean; initialized: boolean; authenticated: boolean; username?: string | null };
type Brief = {
  request: string; title: string; target_audience?: string | null; duration_seconds: number;
  aspect_ratio: string; fps: number; style?: string | null; language: string;
  content_constraints: string[]; audio_required: boolean; shot_duration_seconds: number;
  max_shots: number; budget_usd: number; parallelism_mode: string; parallelism?: number | null;
  resolution_mode: string; resolution_width?: number | null; resolution_height?: number | null;
  acceptance_mode: string; acceptance_custom?: string | null;
};
type ClarificationOption = { id: string; label: string; value: string; explanation: string };
type ClarificationTurn = { id: string; question: string; confirmed: boolean; options?: ClarificationOption[]; skipped?: boolean; answer?: string | null };
type Criterion = { id: string; category: string; statement: string };
type Shot = { id: string; sequence: number; title: string; description: string; duration_seconds?: number; acceptance_criteria: Criterion[]; prompt_bundle?: { positive: string; version: number } };
type Plan = { id: string; version: number; status: string; shots: Shot[]; resolved_settings?: Record<string, unknown>; approved_by?: string | null };
type Artifact = { id: string; kind: string; uri: string; mime_type?: string | null; metadata?: Record<string, unknown> };
type Attempt = {
  id?: string; shot_id: string; number: number; status: string; artifacts?: Artifact[];
  judge_result?: { verdict: string; summary?: string; criterion_results: { criterion_id: string; verdict: string; failure_code?: string; reason?: string }[] };
};
type Project = {
  id: string; name: string; status: string; total_cost_usd: number; brief: Brief;
  clarification_turns: ClarificationTurn[]; plans: Plan[]; attempts: Attempt[]; artifacts: Artifact[];
  version?: number; root_project_id?: string; parent_project_id?: string | null; revision?: number; updated_at?: string;
};
type ProjectSummary = { id: string; name: string; status: string; version: number; root_project_id: string; parent_project_id?: string | null; updated_at: string; total_cost_usd: number };
type EventRecord = { id: number; event_type: string; payload: Record<string, unknown>; created_at: string };
type CustomProvider = {
  id: string; name: string; capability: string; base_url?: string; submit_url?: string; poll_url?: string;
  api_key?: string; model?: string; method?: string; headers?: Record<string, string>;
  body_template?: Record<string, unknown>; poll?: Record<string, unknown>; result?: Record<string, unknown>;
  timeout_seconds?: number; cost_per_second_usd?: number;
};
type SettingsResponse = { settings: Record<string, string | number | boolean>; requires_restart?: string[]; providers?: CustomProvider[]; llm_configured?: boolean; vlm_configured?: boolean };
type ProviderJson = { headers: string; body_template: string; poll: string; result: string };
type Route = 'projects' | 'new' | 'clarify' | 'plan' | 'progress' | 'review' | 'delivery' | 'settings' | 'advanced';

const phaseNames = ['需求澄清', '计划审核', '制作进度', '质量验收', '成片交付'];
const phaseRoutes: Route[] = ['clarify', 'plan', 'progress', 'review', 'delivery'];
const settingLabels: Record<string, string> = {
  VIDEO_PROVIDER: '视频 Provider', VIDEO_MODEL: '视频模型', DIRECTOR_LLM_MODEL: 'LLM 模型',
  DIRECTOR_VLM_MODEL: 'VLM 模型', LLM_PROVIDER: 'LLM 适配器', VLM_PROVIDER: 'VLM 适配器',
  DIRECTOR_PROJECT_BUDGET_USD: '默认项目预算（美元）', DIRECTOR_DEFAULT_DURATION_SECONDS: '默认总时长（秒）',
  DIRECTOR_DEFAULT_SHOT_DURATION_SECONDS: '默认镜头时长（秒）', DIRECTOR_DEFAULT_MAX_SHOTS: '默认最大镜头数',
  DIRECTOR_DEFAULT_ACCEPTANCE_MODE: '默认验收策略', DIRECTOR_PARALLELISM: '默认并发',
  DIRECTOR_AUTH_ENABLED: '登录保护', DIRECTOR_HOST_CHECK_ENABLED: 'Host 限制',
  DIRECTOR_CORS_ENABLED: 'CORS 限制', DIRECTOR_RATE_LIMIT_ENABLED: '请求限流',
  DIRECTOR_CONTENT_SAFETY_ENABLED: '内容安全审核', DIRECTOR_PROVIDER_SAFETY_ENABLED: 'Provider 安全策略',
  DIRECTOR_MAX_ATTEMPTS: '单镜头最大尝试次数', DIRECTOR_DATABASE_URL: '数据库连接',
  REDIS_URL: 'Redis 连接', OBJECT_STORAGE_ENDPOINT: '对象存储连接', OPENAI_BASE_URL: '模型 API 地址',
  OPENAI_API_KEY: 'OpenAI 兼容 API Key', AGNES_API_KEY: 'Agnes API Key',
  AGNES_BACKUP_API_KEY: 'Agnes 备用 API Key',
};
const statusLabels: Record<string, string> = {
  clarifying: '等待需求澄清', awaiting_plan_approval: '等待计划审核', planned: '计划已就绪',
  generating: '正在制作', judging: '正在验收', repairing: '正在修复',
  awaiting_human: '等待人工处理', assembling: '正在组装', delivered: '已交付',
  failed: '执行失败', cancelled: '已取消',
};

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init, credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(String(body.detail || `请求失败（${response.status}）`));
  }
  return response.json() as Promise<T>;
}

function routeFromHash(): Route {
  const value = window.location.hash.replace(/^#\/?/, '').split('/')[0] as Route;
  return ['projects', 'new', 'clarify', 'plan', 'progress', 'review', 'delivery', 'settings', 'advanced'].includes(value) ? value : 'projects';
}

export function App() {
  const [route, setRoute] = useState<Route>(routeFromHash());
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = window.localStorage.getItem('opencine-theme');
    return saved === 'dark' || saved === 'light' ? saved : (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  });
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [loading, setLoading] = useState(true);
  const [authLoading, setAuthLoading] = useState(true);
  const [projectList, setProjectList] = useState<ProjectSummary[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [providers, setProviders] = useState<CustomProvider[]>([]);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [queuedJobId, setQueuedJobId] = useState<string | null>(null);
  const [setupForm, setSetupForm] = useState({ username: '', password: '', confirm: '' });
  const [loginForm, setLoginForm] = useState({ username: '', password: '' });
  const [projectId, setProjectId] = useState<string | null>(() => window.localStorage.getItem('director-project-id'));

  const navigate = useCallback((next: Route) => { window.location.hash = `#/${next}`; setRoute(next); }, []);
  const refreshProject = useCallback(async (id: string) => {
    const [next, nextEvents] = await Promise.all([
      apiFetch<Project>(`/v1/projects/${id}`),
      apiFetch<EventRecord[]>(`/v1/projects/${id}/events`),
    ]);
    setProject(next); setEvents(nextEvents); setProjectId(next.id);
    window.localStorage.setItem('director-project-id', next.id);
    setError(null);
    return next;
  }, []);
  const loadSettings = useCallback(async () => {
    const response = await apiFetch<SettingsResponse>('/v1/settings');
    setSettings(response);
    setProviders(await apiFetch<CustomProvider[]>('/v1/providers/custom').catch(() => []));
  }, []);
  const loadProjects = useCallback(async () => {
    const list = await apiFetch<ProjectSummary[]>('/v1/projects');
    setProjectList(list);
    return list;
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem('opencine-theme', theme);
  }, [theme]);
  useEffect(() => {
    const listener = () => setRoute(routeFromHash());
    window.addEventListener('hashchange', listener);
    return () => window.removeEventListener('hashchange', listener);
  }, []);
  const bootstrap = useCallback(async () => {
    try {
      const status = await apiFetch<{ enabled: boolean; initialized: boolean; username?: string | null }>('/v1/auth/status');
      if (!status.enabled) setAuth({ ...status, authenticated: true });
      else if (!status.initialized) setAuth({ ...status, authenticated: false });
      else {
        try {
          const me = await apiFetch<{ username: string }>('/v1/auth/me');
          setAuth({ ...status, authenticated: true, username: me.username });
        } catch { setAuth({ ...status, authenticated: false }); }
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : '认证服务不可用'); }
    finally { setAuthLoading(false); }
  }, []);
  useEffect(() => { void bootstrap(); }, [bootstrap]);
  useEffect(() => {
    if (!auth?.authenticated) { setLoading(false); return; }
    let cancelled = false;
    (async () => {
      try {
        const list = await loadProjects();
        await loadSettings();
        const selected = projectId && list.some((item) => item.id === projectId) ? projectId : list[0]?.id;
        if (!cancelled && selected) await refreshProject(selected);
        if (!cancelled && !selected) navigate('projects');
      } catch (cause) { if (!cancelled) setError(cause instanceof Error ? cause.message : '导演 API 当前不可用'); }
      finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; };
  }, [auth?.authenticated, loadProjects, loadSettings, navigate, projectId, refreshProject]);
  useEffect(() => {
    if (!project || !auth?.authenticated) return;
    const timer = window.setInterval(() => { void refreshProject(project.id); }, 5000);
    return () => window.clearInterval(timer);
  }, [auth?.authenticated, project?.id, refreshProject]);
  useEffect(() => {
    if (project && !['planned', 'awaiting_plan_approval'].includes(project.status)) setQueuedJobId(null);
  }, [project?.status]);

  const submitAuth = async (mode: 'setup' | 'login') => {
    setRunning(true); setError(null);
    try {
      if (mode === 'setup') {
        if (setupForm.password !== setupForm.confirm) throw new Error('两次密码输入不一致');
        await apiFetch('/v1/auth/setup', { method: 'POST', body: JSON.stringify({ username: setupForm.username, password: setupForm.password }) });
        await apiFetch('/v1/auth/login', { method: 'POST', body: JSON.stringify({ username: setupForm.username, password: setupForm.password }) });
      } else await apiFetch('/v1/auth/login', { method: 'POST', body: JSON.stringify(loginForm) });
      await bootstrap();
    } catch (cause) { setError(cause instanceof Error ? cause.message : '认证失败'); }
    finally { setRunning(false); }
  };
  const selectProject = async (id: string, target?: Route) => {
    setRunning(true);
    setQueuedJobId(null);
    try {
      const next = await refreshProject(id);
      navigate(target || routeForStatus(next.status));
    } catch (cause) { setError(cause instanceof Error ? cause.message : '项目读取失败'); }
    finally { setRunning(false); }
  };
  const createProject = async (payload: Partial<Brief>) => {
    setRunning(true); setError(null);
    setQueuedJobId(null);
    try {
      const next = await apiFetch<Project>('/v1/projects', { method: 'POST', body: JSON.stringify(payload) });
      await loadProjects(); await refreshProject(next.id);
      navigate(next.clarification_turns.some((turn) => !turn.confirmed) ? 'clarify' : 'plan');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '项目创建失败'); }
    finally { setRunning(false); }
  };
  const createVersion = async () => {
    if (!project) return;
    setRunning(true);
    setQueuedJobId(null);
    try {
      const next = await apiFetch<Project>(`/v1/projects/${project.id}/versions`, { method: 'POST', body: JSON.stringify({ actor: auth?.username || 'operator' }) });
      await loadProjects(); await refreshProject(next.id); navigate('clarify');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '新版本创建失败'); }
    finally { setRunning(false); }
  };
  const answerClarifications = async (answers: Record<string, string | { answer?: string; skip?: boolean }>) => {
    if (!project) return;
    setRunning(true);
    try {
      const next = await apiFetch<Project>(`/v1/projects/${project.id}/clarifications`, { method: 'POST', body: JSON.stringify(answers) });
      setProject(next);
      if (next.clarification_turns.every((turn) => turn.confirmed)) navigate('plan');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '需求澄清保存失败'); }
    finally { setRunning(false); }
  };
  const makePlan = async () => {
    if (!project) return;
    setRunning(true);
    try { const next = await apiFetch<Project>(`/v1/projects/${project.id}/plan`, { method: 'POST' }); setProject(next); navigate('plan'); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '计划生成失败'); }
    finally { setRunning(false); }
  };
  const approvePlan = async () => {
    if (!project) return;
    setRunning(true);
    try {
      const next = await apiFetch<Project>(`/v1/projects/${project.id}/approve-plan`, { method: 'POST', body: JSON.stringify({ actor: auth?.username || 'operator' }) });
      setProject(next); navigate('progress');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '计划审核失败'); }
    finally { setRunning(false); }
  };
  const runProject = async () => {
    if (!project) return;
    setRunning(true);
    try {
      const response = await apiFetch<Project | { queued: boolean; job_id: string; project: Project }>(`/v1/projects/${project.id}/run`, { method: 'POST', body: JSON.stringify({ async: true, approve_plan: true, actor: auth?.username || 'operator' }) });
      if ('queued' in response && response.queued) {
        setQueuedJobId(response.job_id);
        setProject(response.project);
        setNotice('制作任务已排队，后端 Worker 会继续处理。');
        navigate('progress');
      } else {
        const next = response as Project;
        setProject(next); navigate(next.status === 'delivered' || next.status === 'awaiting_human' ? 'delivery' : 'progress');
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : '制作任务启动失败'); }
    finally { setRunning(false); }
  };
  const rewind = async (target: string) => {
    if (!project) return;
    if (!window.confirm(`确认回退到“${phaseName(target)}”？下游结果会标记为失效，已产生的费用不会回滚。`)) return;
    setRunning(true);
    try {
      const next = await apiFetch<Project>(`/v1/projects/${project.id}/rewind`, { method: 'POST', body: JSON.stringify({ target_phase: target, actor: auth?.username || 'operator', expected_revision: project.revision }) });
      setProject(next);
      navigate(target === 'requirements' ? 'clarify' : target === 'plan' ? 'plan' : 'progress');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '阶段回退失败'); }
    finally { setRunning(false); }
  };
  const deliver = async () => {
    if (!project) return;
    setRunning(true);
    try { const next = await apiFetch<Project>(`/v1/projects/${project.id}/deliver`, { method: 'POST', body: JSON.stringify({ actor: auth?.username || 'operator' }) }); setProject(next); await loadProjects(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '交付确认失败'); }
    finally { setRunning(false); }
  };

  if (authLoading || loading) return <div className="loading-screen"><LoaderCircle className="spin" size={20} />正在读取 OpenCine-AI</div>;
  if (auth && auth.enabled && !auth.authenticated) {
    return <AuthPage setup={!auth.initialized} form={auth.initialized ? loginForm : setupForm} setForm={auth.initialized ? (value) => setLoginForm({ username: value.username, password: value.password }) : (value) => setSetupForm({ username: value.username, password: value.password, confirm: value.confirm || '' })} onSubmit={() => void submitAuth(auth.initialized ? 'login' : 'setup')} error={error} running={running} />;
  }

  const activeProject = project;
  const pageTitle = route === 'new' ? '新建项目' : route === 'projects' ? '项目' : route === 'settings' ? '设置' : route === 'advanced' ? '高级设置' : route === 'delivery' ? '成片预览' : activeProject?.name || 'OpenCine-AI';
  return <div className="app-shell">
    <aside className="sidebar">
      <button className="brand-button" onClick={() => navigate('projects')}><img src="/opencine-ai-logo.png" alt="OpenCine-AI" /><span><strong>OpenCine-AI</strong><small>AI 视频制作系统</small></span></button>
      <nav aria-label="主导航">
        <NavItem active={route === 'projects'} icon={<FolderOpen size={16} />} label="项目" onClick={() => navigate('projects')} />
        <NavItem active={route === 'new'} icon={<Plus size={16} />} label="新建项目" onClick={() => navigate('new')} />
        <NavItem active={['clarify', 'plan', 'progress', 'review', 'delivery'].includes(route)} icon={<Activity size={16} />} label="制作流程" onClick={() => navigate(route === 'projects' ? 'clarify' : route)} disabled={!activeProject} />
        <NavItem active={route === 'settings'} icon={<Settings2 size={16} />} label="设置" onClick={() => navigate('settings')} />
        <NavItem active={route === 'advanced'} icon={<ShieldCheck size={16} />} label="高级设置" onClick={() => navigate('advanced')} />
      </nav>
      <div className="sidebar-bottom"><span className="connection"><i />后端已连接</span><button className="nav-item" onClick={() => void apiFetch('/v1/auth/logout', { method: 'POST' }).finally(() => setAuth((value) => value ? { ...value, authenticated: false } : value))}><LogOut size={16} />退出登录</button></div>
    </aside>
    <main className="main">
      <header className="topbar"><div><p className="kicker">{activeProject ? `项目 / ${activeProject.name}` : 'OpenCine-AI'}</p><h1>{pageTitle}</h1></div><div className="top-actions"><button className="icon-button" title={theme === 'light' ? '切换到暗色模式' : '切换到亮色模式'} aria-label={theme === 'light' ? '切换到暗色模式' : '切换到亮色模式'} onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}>{theme === 'light' ? <Moon size={18} /> : <Sun size={18} />}</button><button className="icon-button" title="刷新项目状态" aria-label="刷新项目状态" onClick={() => activeProject && void refreshProject(activeProject.id)}><RefreshCw size={18} /></button>{activeProject && <span className="status-chip"><i />{statusLabels[activeProject.status] || activeProject.status}</span>}</div></header>
      {activeProject && <StageBar project={activeProject} route={route} onNavigate={navigate} onRewind={(target) => void rewind(target)} />}
      {notice && <div className="notice"><Check size={16} />{notice}</div>}
      {error && <div className="notice error"><CircleAlert size={16} /><span>{error}</span><button onClick={() => setError(null)} aria-label="关闭错误">关闭</button></div>}
      <div className="page-content">
        {route === 'projects' && <ProjectsPage projects={projectList} selected={activeProject?.id} onSelect={(id) => void selectProject(id)} onCreate={() => navigate('new')} />}
        {route === 'new' && <NewProjectPage onCreate={(payload) => void createProject(payload)} running={running} defaults={settings?.settings} />}
        {route === 'clarify' && activeProject && <ClarificationPage project={activeProject} onSubmit={(answers) => void answerClarifications(answers)} running={running} onBack={() => navigate('projects')} />}
        {route === 'plan' && activeProject && <PlanPage project={activeProject} onGenerate={() => void makePlan()} onApprove={() => void approvePlan()} running={running} />}
        {route === 'progress' && activeProject && <ProgressPage project={activeProject} events={events} onRun={() => void runProject()} onReview={() => navigate('review')} running={running} queued={queuedJobId !== null} />}
        {route === 'review' && activeProject && <ReviewPage project={activeProject} onRetry={() => void refreshProject(activeProject.id)} onDelivery={() => navigate('delivery')} />}
        {route === 'delivery' && activeProject && <DeliveryPage project={activeProject} onDeliver={() => void deliver()} onNewVersion={() => void createVersion()} running={running} />}
        {route === 'settings' && <SettingsPage mode="basic" settings={settings} providers={providers} onReload={() => void loadSettings()} onSaved={(next) => setSettings(next)} />}
        {route === 'advanced' && <SettingsPage mode="advanced" settings={settings} providers={providers} onReload={() => void loadSettings()} onSaved={(next) => setSettings(next)} />}
      </div>
    </main>
  </div>;
}

function AuthPage({ setup, form, setForm, onSubmit, error, running }: { setup: boolean; form: { username: string; password: string; confirm?: string }; setForm: (value: { username: string; password: string; confirm?: string }) => void; onSubmit: () => void; error: string | null; running: boolean }) {
  return <main className="auth-screen"><section className="auth-panel"><div className="auth-brand"><img src="/opencine-ai-logo.png" alt="OpenCine-AI" /><div><strong>OpenCine-AI</strong><small>AI 视频制作系统</small></div></div><p className="kicker">{setup ? '首次使用' : '管理员登录'}</p><h1>{setup ? '设置管理员账号' : '登录制作空间'}</h1><p className="muted">{setup ? '先设置一个本地管理员账号，之后即可进入制作空间。' : '请输入管理员账号继续。'}</p>{error && <div className="notice error"><CircleAlert size={16} />{error}</div>}<label>用户名<input value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} /></label><label>密码<input type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} /></label>{setup && <label>确认密码<input type="password" value={form.confirm || ''} onChange={(event) => setForm({ ...form, confirm: event.target.value })} /></label>}<button className="primary full" disabled={running || form.password.length < 8 || !form.username.trim()} onClick={onSubmit}><KeyRound size={16} />{setup ? '完成初始化' : '登录'}</button></section></main>;
}

function NavItem({ active, icon, label, onClick, disabled }: { active: boolean; icon: ReactNode; label: string; onClick: () => void; disabled?: boolean }) { return <button className={`nav-item ${active ? 'active' : ''}`} aria-current={active ? 'page' : undefined} disabled={disabled} onClick={onClick}>{icon}<span>{label}</span></button>; }

function StageBar({ project, route, onNavigate, onRewind }: { project: Project; route: Route; onNavigate: (route: Route) => void; onRewind: (target: string) => void }) {
  const current = phaseIndex(project.status); const active = phaseRoutes.indexOf(route);
  return <div className="stage-bar">{phaseNames.map((name, index) => { const done = index < current; const selected = active === index || (route === 'projects' && index === 0); const target = phaseRoutes[index]; return <button key={name} className={`stage ${done ? 'done' : ''} ${selected ? 'selected' : ''}`} onClick={() => { if (index <= current) { if (index < current) onRewind(phaseKey(index)); else onNavigate(target); } }} disabled={index > current} aria-current={selected ? 'step' : undefined}><span>{done ? <Check size={13} /> : index + 1}</span><b>{name}</b>{index < phaseNames.length - 1 && <ArrowRight size={14} />}</button>; })}</div>;
}

function ProjectsPage({ projects, selected, onSelect, onCreate }: { projects: ProjectSummary[]; selected?: string; onSelect: (id: string) => void; onCreate: () => void }) {
  return <section className="page-stack"><div className="page-intro"><div><p className="kicker">工作空间</p><h2>项目与交付版本</h2><p className="muted">每个项目都可以继续制作，也可以从已交付版本创建新的交付版本。</p></div><button className="primary" onClick={onCreate}><Plus size={16} />新建项目</button></div><section className="panel table-panel"><div className="panel-head table-head"><span>项目</span><span>状态</span><span>版本</span><span>更新时间</span><span>费用</span><span /></div>{projects.length ? projects.map((item) => <button className={`project-row ${selected === item.id ? 'selected' : ''}`} key={item.id} onClick={() => onSelect(item.id)}><span><strong>{item.name}</strong><small>{item.id}</small></span><span><StatusBadge status={item.status} /></span><span>v{item.version}</span><span>{formatDate(item.updated_at)}</span><span>${item.total_cost_usd.toFixed(2)}</span><ArrowRight size={16} /></button>) : <EmptyState title="还没有项目" detail="从一条创作提示词开始。" action="新建项目" onAction={onCreate} />}</section></section>;
}

function NewProjectPage({ onCreate, running, defaults }: { onCreate: (payload: Partial<Brief>) => void; running: boolean; defaults?: Record<string, string | number | boolean> }) {
  const [form, setForm] = useState({ title: '', request: '', duration_seconds: Number(defaults?.DIRECTOR_DEFAULT_DURATION_SECONDS || 30), shot_duration_seconds: Number(defaults?.DIRECTOR_DEFAULT_SHOT_DURATION_SECONDS || 15), max_shots: Number(defaults?.DIRECTOR_DEFAULT_MAX_SHOTS || 2), budget_usd: Number(defaults?.DIRECTOR_PROJECT_BUDGET_USD || 75), fps: 24, aspect_ratio: '16:9', style: '', language: 'zh-CN', acceptance_mode: String(defaults?.DIRECTOR_DEFAULT_ACCEPTANCE_MODE || 'standard') });
  const update = (key: string, value: string | number) => setForm((current) => ({ ...current, [key]: value }));
  return <section className="page-stack narrow-page"><div className="page-intro"><div><p className="kicker">第一步</p><h2>先告诉导演你想做什么</h2><p className="muted">先输入原始创作提示词。系统会根据内容主动提出影响成片的问题。</p></div></div><section className="panel form-panel"><label className="wide-field">项目名称<input value={form.title} placeholder="例如：雨夜归信" onChange={(event) => update('title', event.target.value)} /></label><label className="wide-field">原始创作提示词<textarea autoFocus rows={8} value={form.request} placeholder="描述故事、人物、画面、节奏、声音或你已经确定的任何要求。" onChange={(event) => update('request', event.target.value)} /></label><div className="form-grid"><label>总时长（秒）<input type="number" min="1" value={form.duration_seconds} onChange={(event) => update('duration_seconds', Number(event.target.value))} /></label><label>单镜头时长（秒）<input type="number" min="1" value={form.shot_duration_seconds} onChange={(event) => update('shot_duration_seconds', Number(event.target.value))} /></label><label>最大镜头数<input type="number" min="1" value={form.max_shots} onChange={(event) => update('max_shots', Number(event.target.value))} /></label><label>预算（美元）<input type="number" min="0" value={form.budget_usd} onChange={(event) => update('budget_usd', Number(event.target.value))} /></label><label>帧率<select value={form.fps} onChange={(event) => update('fps', Number(event.target.value))}><option value="24">24 fps</option><option value="30">30 fps</option><option value="60">60 fps</option></select></label><label>画幅<select value={form.aspect_ratio} onChange={(event) => update('aspect_ratio', event.target.value)}><option value="16:9">16:9</option><option value="9:16">9:16</option><option value="1:1">1:1</option></select></label><label>语言<select value={form.language} onChange={(event) => update('language', event.target.value)}><option value="zh-CN">中文</option><option value="en-US">English</option></select></label><label>视觉风格<input value={form.style} placeholder="可留空，让澄清阶段继续判断" onChange={(event) => update('style', event.target.value)} /></label></div><div className="form-actions"><button className="primary" disabled={running || !form.request.trim()} onClick={() => onCreate({ ...form, title: form.title.trim() || '未命名项目', style: form.style.trim() || null, content_constraints: [], audio_required: true, parallelism_mode: 'auto', resolution_mode: 'auto' })}><ArrowRight size={16} />进入需求澄清</button></div></section></section>;
}

function ClarificationPage({ project, onSubmit, running, onBack }: { project: Project; onSubmit: (answers: Record<string, string | { answer?: string; skip?: boolean }>) => void; running: boolean; onBack: () => void }) {
  const turns = project.clarification_turns.filter((turn) => !turn.confirmed);
  const [answers, setAnswers] = useState<Record<string, string | { answer?: string; skip?: boolean }>>({});
  return <section className="page-stack narrow-page"><div className="page-intro split"><div><p className="kicker">第二步</p><h2>需求澄清</h2><p className="muted">每轮最多展示 3 个高影响问题。回答后，LLM 会判断是否还需要继续澄清。</p></div><span className="progress-count">{turns.length} 项待确认</span></div>{turns.length === 0 ? <section className="panel success-panel"><Check size={24} /><div><h3>需求信息已经足够</h3><p className="muted">可以进入计划审核。</p></div><button className="primary" onClick={() => onSubmit({})}>查看计划 <ArrowRight size={16} /></button></section> : <section className="panel form-panel">{turns.slice(0, 3).map((turn) => { const current = answers[turn.id]; const text = typeof current === 'string' ? current : current?.answer || ''; return <div className="question" key={turn.id}><strong>{turn.question}</strong>{turn.options?.length ? <div className="option-list">{turn.options.map((option) => <label className={`option ${text === option.value ? 'chosen' : ''}`} key={option.id}><input type="radio" name={turn.id} checked={text === option.value} onChange={() => setAnswers({ ...answers, [turn.id]: option.value })} /><span><b>{option.label}</b><small>{option.explanation}</small></span></label>)}</div> : null}<input value={text} placeholder="也可以输入自己的回答" onChange={(event) => setAnswers({ ...answers, [turn.id]: event.target.value })} /></div>; })}<div className="form-actions"><button className="secondary" onClick={onBack}><ArrowLeft size={16} />返回项目</button><button className="primary" disabled={running || turns.slice(0, 3).some((turn) => { const value = answers[turn.id]; return !value || (typeof value === 'string' && !value.trim()); })} onClick={() => onSubmit(answers)}><Check size={16} />提交回答</button></div></section>}</section>;
}

function PlanPage({ project, onGenerate, onApprove, running }: { project: Project; onGenerate: () => void; onApprove: () => void; running: boolean }) {
  const plan = activePlan(project);
  return <section className="page-stack"><div className="page-intro split"><div><p className="kicker">第三步</p><h2>计划审核</h2><p className="muted">先看镜头顺序、提示词和验收标准，再开始制作。</p></div>{plan && <StatusBadge status={plan.status} />}</div>{!plan ? <EmptyState title="计划还没有生成" detail="需求澄清完成后，生成一版可审核计划。" action="生成计划" onAction={onGenerate} /> : <><section className="panel plan-summary"><div><span className="summary-label">镜头数量</span><strong>{plan.shots.length}</strong></div><div><span className="summary-label">总时长</span><strong>{project.brief.duration_seconds}s</strong></div><div><span className="summary-label">验收策略</span><strong>{acceptanceLabel(project.brief.acceptance_mode)}</strong></div><button className="primary" disabled={running || plan.status === 'approved'} onClick={onApprove}>{plan.status === 'approved' ? '计划已审核' : '审核并开始制作'} <ArrowRight size={16} /></button></section><section className="shot-cards">{plan.shots.map((shot) => <article className="shot-card" key={shot.id}><span className="shot-index">{String(shot.sequence).padStart(2, '0')}</span><div><h3>{shot.title}</h3><p>{shot.description}</p><span className="criteria-count">{shot.acceptance_criteria.length} 项验收标准</span></div><ArrowRight size={16} /></article>)}</section></>}</section>;
}

function ProgressPage({ project, events, onRun, onReview, running, queued }: { project: Project; events: EventRecord[]; onRun: () => void; onReview: () => void; running: boolean; queued: boolean }) {
  const plan = activePlan(project); const latest = latestAttempts(project);
  return <section className="page-stack"><div className="page-intro split"><div><p className="kicker">第四步</p><h2>制作进度</h2><p className="muted">生成、关键帧验收和组装会在这里持续更新。</p></div><button className="primary" onClick={project.status === 'awaiting_human' ? onReview : onRun} disabled={running || queued || ['generating', 'judging', 'repairing', 'assembling'].includes(project.status)}>{queued ? '制作已排队' : project.status === 'awaiting_human' ? '查看验收' : project.status === 'delivered' ? '查看成片' : '开始制作'}<Play size={15} /></button></div><section className="metrics"><Metric label="镜头" value={`${Object.keys(latest).length}/${plan?.shots.length || 0}`} /><Metric label="通过" value={`${Object.values(latest).filter((attempt) => attempt.judge_result?.verdict === 'PASS').length}`} /><Metric label="累计费用" value={`$${project.total_cost_usd.toFixed(2)}`} /><Metric label="当前状态" value={statusLabels[project.status] || project.status} /></section><div className="split-grid"><section className="panel"><div className="panel-head"><h3>镜头队列</h3><span>{plan?.shots.length || 0} 个镜头</span></div><div className="shot-list">{plan?.shots.map((shot) => { const attempt = latest[shot.id]; return <div className="shot-row" key={shot.id}><span className="shot-index">{String(shot.sequence).padStart(2, '0')}</span><span><strong>{shot.title}</strong><small>{attempt ? verdictLabel(attempt.judge_result?.verdict || attempt.status) : '尚未开始'}</small></span><StatusBadge status={attempt?.judge_result?.verdict || attempt?.status || 'queued'} /></div>; }) || <EmptyState title="等待计划" detail="审核计划后开始制作。" />}</div></section><section className="panel"><div className="panel-head"><h3>运行记录</h3><button className="text-button" onClick={onReview}>查看验收 <ArrowRight size={14} /></button></div><div className="event-list">{events.slice(-8).reverse().map((event) => <div className="event-row" key={event.id}><span className="event-dot" /><div><strong>{eventLabel(event.event_type)}</strong><small>{eventSummary(event)}</small></div><time>{formatTime(event.created_at)}</time></div>)}{!events.length && <EmptyState title="还没有运行记录" detail="开始制作后会显示状态变化。" />}</div></section></div></section>;
}

function ReviewPage({ project, onRetry, onDelivery }: { project: Project; onRetry: () => void; onDelivery: () => void }) {
  const plan = activePlan(project); const latest = latestAttempts(project);
  return <section className="page-stack"><div className="page-intro split"><div><p className="kicker">质量验收</p><h2>镜头与证据</h2><p className="muted">验收结果来自模型返回的证据。缺少证据时会保持失败，等待人工处理。</p></div>{project.status === 'awaiting_human' && <button className="primary" onClick={onDelivery}>进入成片预览 <ArrowRight size={16} /></button>}</div><section className="review-list">{plan?.shots.map((shot) => { const attempt = latest[shot.id]; return <article className="panel review-card" key={shot.id}><div className="review-head"><div><span className="shot-index">镜头 {String(shot.sequence).padStart(2, '0')}</span><h3>{shot.title}</h3></div><StatusBadge status={attempt?.judge_result?.verdict || 'PENDING'} /></div><p>{shot.description}</p><div className="criterion-list">{shot.acceptance_criteria.map((criterion) => { const result = attempt?.judge_result?.criterion_results.find((item) => item.criterion_id === criterion.id); return <div className="criterion-row" key={criterion.id}><span className={result?.verdict === 'PASS' ? 'pass' : 'pending'}>{result?.verdict === 'PASS' ? <Check size={14} /> : <CircleAlert size={14} />}</span><span>{criterion.statement}</span><small>{verdictLabel(result?.verdict || 'PENDING')}</small></div>; })}</div><button className="secondary" onClick={onRetry}><RotateCcw size={15} />刷新验收结果</button></article>; }) || <EmptyState title="还没有可验收的镜头" detail="开始制作后会显示验收详情。" />}</section></section>;
}

function DeliveryPage({ project, onDeliver, onNewVersion, running }: { project: Project; onDeliver: () => void; onNewVersion: () => void; running: boolean }) {
  const delivery = activeDelivery(project); const mediaUrl = delivery ? `/v1/projects/${project.id}/artifacts/${delivery.id}/stream` : '';
  return <section className="page-stack delivery-page"><div className="page-intro split"><div><p className="kicker">第五步</p><h2>成片预览</h2><p className="muted">在这里检查最终成片，确认后完成交付。</p></div>{project.status === 'delivered' ? <StatusBadge status="delivered" /> : <StatusBadge status={project.status} />}</div><section className="panel video-panel">{mediaUrl ? <video controls preload="metadata" src={mediaUrl}>你的浏览器不支持视频播放。</video> : <div className="media-empty"><Film size={30} /><strong>成片尚未生成</strong><span>完成制作和组装后，视频会出现在这里。</span></div>}<div className="video-meta"><div><span className="summary-label">项目</span><strong>{project.name}</strong></div><div><span className="summary-label">版本</span><strong>v{project.version || 1}</strong></div><div><span className="summary-label">总时长</span><strong>{project.brief.duration_seconds}s</strong></div></div></section><div className="form-actions delivery-actions">{delivery && <a className="primary" href={`/v1/projects/${project.id}/artifacts/${delivery.id}/download`}><Download size={16} />下载 MP4</a>}{project.status === 'awaiting_human' && delivery && <button className="primary" disabled={running} onClick={onDeliver}><Check size={16} />确认交付</button>}{project.status === 'delivered' && <button className="secondary" disabled={running} onClick={onNewVersion}><Plus size={16} />创建新版本</button>}<button className="secondary" onClick={() => window.open(mediaUrl, '_blank')} disabled={!mediaUrl}><ExternalLink size={15} />新窗口预览</button></div></section>;
}

function SettingsPage({ mode, settings, providers, onReload, onSaved }: { mode: 'basic' | 'advanced'; settings: SettingsResponse | null; providers: CustomProvider[]; onReload: () => void; onSaved: (settings: SettingsResponse) => void }) {
  const [draft, setDraft] = useState<Record<string, string | number | boolean>>({}); const [providerDraft, setProviderDraft] = useState<CustomProvider | null>(null); const [providerJson, setProviderJson] = useState<ProviderJson>({ headers: '{}', body_template: '{}', poll: '{}', result: '{}' }); const [saving, setSaving] = useState(false); const [message, setMessage] = useState('');
  useEffect(() => { if (settings) setDraft(settings.settings); }, [settings]);
  const advancedKeys = ['DIRECTOR_AUTH_ENABLED', 'DIRECTOR_HOST_CHECK_ENABLED', 'DIRECTOR_CORS_ENABLED', 'DIRECTOR_RATE_LIMIT_ENABLED', 'DIRECTOR_CONTENT_SAFETY_ENABLED', 'DIRECTOR_PROVIDER_SAFETY_ENABLED', 'DIRECTOR_MAX_ATTEMPTS', 'DIRECTOR_DATABASE_URL', 'REDIS_URL', 'OBJECT_STORAGE_ENDPOINT'];
  const keys = Object.keys(draft).filter((key) => mode === 'basic' ? !advancedKeys.includes(key) : advancedKeys.includes(key));
  const save = async () => {
    setSaving(true);
    try {
      const response = await apiFetch<SettingsResponse>(`/v1/settings/${mode}`, { method: 'PATCH', body: JSON.stringify(Object.fromEntries(keys.map((key) => [key, draft[key]]))) });
      onSaved(response); setMessage('设置已保存。新任务会读取最新的普通设置。');
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : '设置保存失败'); }
    finally { setSaving(false); }
  };
  const startProvider = (provider?: CustomProvider) => {
    const value = provider ? { ...provider, api_key: '' } : { id: '', name: '', capability: 'video', method: 'POST', submit_url: '', api_key: '', model: '', timeout_seconds: 45 };
    setProviderDraft(value);
    setProviderJson({ headers: JSON.stringify(provider?.headers || {}, null, 2), body_template: JSON.stringify(provider?.body_template || {}, null, 2), poll: JSON.stringify(provider?.poll || {}, null, 2), result: JSON.stringify(provider?.result || {}, null, 2) });
  };
  const saveProvider = async () => {
    if (!providerDraft) return;
    setSaving(true);
    try {
      const payload = { ...providerDraft, headers: JSON.parse(providerJson.headers), body_template: JSON.parse(providerJson.body_template), poll: JSON.parse(providerJson.poll), result: JSON.parse(providerJson.result) };
      const exists = providers.some((item) => item.id === providerDraft.id);
      await apiFetch(`/v1/providers/custom${exists ? `/${providerDraft.id}` : ''}`, { method: exists ? 'PATCH' : 'POST', body: JSON.stringify(payload) });
      setProviderDraft(null); onReload(); setMessage('Provider 已保存。');
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : 'Provider 保存失败'); }
    finally { setSaving(false); }
  };
  const removeProvider = async (id: string) => { if (!window.confirm('确认删除这个 Provider？')) return; await apiFetch(`/v1/providers/custom/${id}`, { method: 'DELETE' }); onReload(); };
  return <section className="page-stack settings-page"><div className="page-intro split"><div><p className="kicker">{mode === 'basic' ? '工作参数' : '运行参数'}</p><h2>{mode === 'basic' ? '设置' : '高级设置'}</h2><p className="muted">{mode === 'basic' ? '配置模型、预算和默认制作参数。' : '配置数据库、认证、限流和安全策略。'}</p></div><button className="primary" disabled={saving} onClick={save}><Save size={16} />保存设置</button></div>{message && <div className="notice"><Check size={16} />{message}</div>}<section className="panel settings-panel"><div className="settings-grid">{keys.map((key) => <SettingField key={key} name={key} value={draft[key]} onChange={(value) => setDraft({ ...draft, [key]: value })} />)}</div>{settings?.requires_restart?.length && mode === 'advanced' ? <p className="settings-note">这些连接配置通常需要重启后端才会完全生效：{settings.requires_restart.join('、')}</p> : null}</section>{mode === 'basic' && <section className="panel provider-panel"><div className="panel-head"><div><h3>自定义 Provider</h3><p className="muted">用请求模板和 JSONPath 接入兼容接口，密钥只显示脱敏结果。</p></div><button className="secondary" onClick={() => startProvider()}><Plus size={15} />添加 Provider</button></div>{providerDraft && <ProviderEditor draft={providerDraft} setDraft={setProviderDraft} json={providerJson} setJson={setProviderJson} saving={saving} onSave={() => void saveProvider()} onCancel={() => setProviderDraft(null)} />}{providers.length ? providers.map((provider) => <div className="provider-row" key={provider.id}><span><strong>{provider.name}</strong><small>{provider.id} · {provider.capability} · {provider.api_key || '未配置密钥'}</small></span><div><button className="icon-button" title="编辑 Provider" onClick={() => startProvider(provider)}><Settings2 size={16} /></button><button className="icon-button danger-icon" title="删除 Provider" onClick={() => void removeProvider(provider.id)}><Trash2 size={16} /></button></div></div>) : <EmptyState title="还没有自定义 Provider" detail="需要接入其他视频或模型服务时，再添加即可。" />}</section>}</section>;
}

function SettingField({ name, value, onChange }: { name: string; value: string | number | boolean; onChange: (value: string | number | boolean) => void }) {
  const isBool = typeof value === 'boolean'; const isNumber = typeof value === 'number';
  return <label className={isBool ? 'toggle-field' : ''}>{isBool ? <><span>{settingLabels[name] || name}</span><input type="checkbox" checked={value} onChange={(event) => onChange(event.target.checked)} /></> : <>{settingLabels[name] || name}<input type={isNumber ? 'number' : name.includes('KEY') ? 'password' : 'text'} value={String(value ?? '')} placeholder={name.includes('KEY') ? '已配置时留空' : undefined} onChange={(event) => onChange(isNumber ? Number(event.target.value) : event.target.value)} /></>}</label>;
}

function ProviderEditor({ draft, setDraft, json, setJson, saving, onSave, onCancel }: { draft: CustomProvider; setDraft: (value: CustomProvider) => void; json: ProviderJson; setJson: (value: ProviderJson) => void; saving: boolean; onSave: () => void; onCancel: () => void }) {
  const update = (key: keyof CustomProvider, value: string | number) => setDraft({ ...draft, [key]: value });
  return <div className="provider-editor"><div className="form-grid"><label>ID<input value={draft.id} onChange={(event) => update('id', event.target.value)} /></label><label>名称<input value={draft.name} onChange={(event) => update('name', event.target.value)} /></label><label>能力<select value={draft.capability} onChange={(event) => update('capability', event.target.value)}><option value="video">视频</option><option value="llm">LLM</option><option value="vlm">VLM</option><option value="audio">音频</option><option value="reference">素材引用</option></select></label><label>请求方法<select value={draft.method || 'POST'} onChange={(event) => update('method', event.target.value)}><option>POST</option><option>GET</option><option>PUT</option></select></label><label className="span-2">提交 URL<input value={draft.submit_url || draft.base_url || ''} onChange={(event) => update('submit_url', event.target.value)} /></label><label>轮询 URL<input value={draft.poll_url || ''} onChange={(event) => update('poll_url', event.target.value)} /></label><label>模型<input value={draft.model || ''} onChange={(event) => update('model', event.target.value)} /></label><label>API Key<input type="password" value={draft.api_key || ''} placeholder="已配置时留空" onChange={(event) => update('api_key', event.target.value)} /></label><label>超时（秒）<input type="number" value={draft.timeout_seconds || 45} onChange={(event) => update('timeout_seconds', Number(event.target.value))} /></label><JsonField label="Headers JSON" value={json.headers} onChange={(value) => setJson({ ...json, headers: value })} /><JsonField label="Body 模板 JSON" value={json.body_template} onChange={(value) => setJson({ ...json, body_template: value })} /><JsonField label="轮询映射 JSON" value={json.poll} onChange={(value) => setJson({ ...json, poll: value })} /><JsonField label="结果 JSONPath" value={json.result} onChange={(value) => setJson({ ...json, result: value })} /></div><div className="form-actions"><button className="secondary" onClick={onCancel}>取消</button><button className="primary" disabled={saving || !draft.id || !draft.name || !(draft.submit_url || draft.base_url)} onClick={onSave}><Save size={15} />保存 Provider</button></div></div>;
}
function JsonField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) { return <label className="span-2">{label}<textarea rows={5} value={value} onChange={(event) => onChange(event.target.value)} /></label>; }
function Metric({ label, value }: { label: string; value: string }) { return <div className="metric"><span>{label}</span><strong>{value}</strong></div>; }
function StatusBadge({ status }: { status: string }) { const normalized = status.toLowerCase(); return <span className={`status-badge ${normalized.includes('pass') || normalized === 'delivered' ? 'pass' : normalized.includes('fail') ? 'fail' : ''}`}><i />{statusLabels[normalized] || verdictLabel(status)}</span>; }
function EmptyState({ title, detail, action, onAction }: { title: string; detail: string; action?: string; onAction?: () => void }) { return <div className="empty-state"><FolderOpen size={22} /><strong>{title}</strong><span>{detail}</span>{action && onAction && <button className="secondary" onClick={onAction}>{action}</button>}</div>; }
function activePlan(project: Project): Plan | undefined { return [...(project.plans || [])].reverse().find((plan) => plan.status !== 'obsolete'); }
function activeDelivery(project: Project): Artifact | undefined { return [...(project.artifacts || [])].reverse().find((artifact) => artifact.kind === 'video' && artifact.metadata?.artifact_role === 'delivery' && artifact.metadata?.active === true); }
function latestAttempts(project: Project): Record<string, Attempt> { return (project.attempts || []).reduce<Record<string, Attempt>>((result, attempt) => { if (!result[attempt.shot_id] || attempt.number > result[attempt.shot_id].number) result[attempt.shot_id] = attempt; return result; }, {}); }
function routeForStatus(status: string): Route { if (status === 'clarifying') return 'clarify'; if (status === 'awaiting_plan_approval' || status === 'planned') return 'plan'; if (status === 'delivered') return 'delivery'; if (status === 'awaiting_human') return 'review'; return 'progress'; }
function phaseIndex(status: string): number { if (status === 'clarifying') return 0; if (status === 'awaiting_plan_approval' || status === 'planned') return 1; if (['generating', 'judging', 'repairing'].includes(status)) return 2; if (status === 'awaiting_human' || status === 'assembling') return 3; if (status === 'delivered') return 4; return 0; }
function phaseKey(index: number): string { return ['requirements', 'plan', 'generation', 'review', 'repair'][index] || 'requirements'; }
function phaseName(value: string): string { return ({ requirements: '需求澄清', plan: '计划审核', generation: '制作进度', review: '质量验收', repair: '修复' } as Record<string, string>)[value] || value; }
function verdictLabel(value: string): string { return ({ PASS: '通过', FAIL: '失败', PENDING: '待验收', queued: '排队中', submitted: '已提交', running: '制作中', generated: '已生成', judged_failed: '验收未通过' } as Record<string, string>)[value] || value; }
function acceptanceLabel(value: string): string { return ({ auto: '自动', low: '低严格', standard: '标准', strict: '严格', custom: '自定义', none: '关闭语义验收' } as Record<string, string>)[value] || value; }
function eventLabel(value: string): string { return ({ 'project.created': '项目已创建', 'project.version.created': '新版本已创建', 'project.rewound': '项目已回退', 'plan.created': '计划已生成', 'plan.approved': '计划已审核', 'generation.started': '开始制作', 'shot.generated': '镜头已生成', 'shot.judged': '镜头已验收', 'project.delivered': '项目已交付' } as Record<string, string>)[value] || value; }
function eventSummary(event: EventRecord): string { if (event.payload.target_phase) return `回退到${phaseName(String(event.payload.target_phase))}`; if (event.payload.error) return String(event.payload.error); if (event.payload.verdict) return `结果：${verdictLabel(String(event.payload.verdict))}`; return '状态已保存'; }
function formatDate(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString('zh-CN'); }
function formatTime(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }); }
