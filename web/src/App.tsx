import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  Activity,
  ArrowRight,
  Check,
  CircleAlert,
  Film,
  Gauge,
  GitBranch,
  Inbox,
  LoaderCircle,
  Play,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  WalletCards,
} from 'lucide-react';

type Criterion = {
  id: string;
  statement: string;
  category: string;
  blocking: boolean;
  severity?: string;
};

type CriterionResult = {
  criterion_id: string;
  verdict: string;
  reason?: string;
  failure_code?: string;
};

type JudgeResult = {
  verdict: string;
  criterion_results?: CriterionResult[];
};

type Shot = {
  id: string;
  sequence: number;
  title: string;
  description: string;
  acceptance_criteria: Criterion[];
  prompt_bundle?: { positive: string; version: number };
};

type Attempt = {
  id: string;
  shot_id: string;
  number: number;
  status: string;
  judge_result?: JudgeResult | null;
};

type Project = {
  id: string;
  name: string;
  status: string;
  total_cost_usd: number;
  brief: { budget_usd: number };
  clarification_turns: { id: string; question: string; confirmed: boolean }[];
  plans: { shots: Shot[] }[];
  attempts: Attempt[];
  artifacts: { uri: string }[];
};

type EventRecord = {
  id: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

type NavSection = 'control' | 'projects' | 'review';

const phases = ['澄清', '规划', '生成', '验收', '修复', '组装'];
const briefPayload = {
  title: '雨中的最后一封信',
  request: '创作一部电影感的 10 镜头短片：主角在雨夜城市里捡到一封遗失的信，循着线索追查，并在最后的黎明时分将信送回失主手中，配合中文旁白、音乐和音效。',
  duration_seconds: 150,
  max_shots: 10,
  shot_duration_seconds: 15,
  aspect_ratio: '16:9',
  fps: 24,
  style: '电影感、蓝调时刻写实风格',
  language: 'zh-CN',
  audio_required: true,
  budget_usd: 75,
};

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  if (!response.ok) {
    throw new Error((await response.text()) || `请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export function App() {
  const [project, setProject] = useState<Project | null>(null);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [selected, setSelected] = useState(0);
  const [running, setRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState('正在连接导演运行时…');
  const [error, setError] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [activeNav, setActiveNav] = useState<NavSection>('control');

  const refresh = useCallback(async (projectId: string) => {
    const [next, nextEvents] = await Promise.all([
      apiFetch<Project>(`/v1/projects/${projectId}`),
      apiFetch<EventRecord[]>(`/v1/projects/${projectId}/events`),
    ]);
    setProject(next);
    setEvents(nextEvents);
    const count = next.plans.length ? next.plans[next.plans.length - 1].shots.length : 1;
    setSelected((current) => Math.min(current, Math.max(0, count - 1)));
    const open = next.clarification_turns.filter((turn) => !turn.confirmed).length;
    setNotice(open ? `${open} 项需求信息待确认` : statusLabel(next.status));
    setError(null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const initialize = async () => {
      try {
        let projectId = window.localStorage.getItem('director-project-id');
        let next: Project;
        if (projectId) {
          try {
            next = await apiFetch<Project>(`/v1/projects/${projectId}`);
          } catch {
            next = await apiFetch<Project>('/v1/projects', {
              method: 'POST',
              body: JSON.stringify(briefPayload),
            });
            projectId = next.id;
          }
        } else {
          next = await apiFetch<Project>('/v1/projects', {
            method: 'POST',
            body: JSON.stringify(briefPayload),
          });
          projectId = next.id;
        }
        if (cancelled) return;
        window.localStorage.setItem('director-project-id', projectId);
        await refresh(next.id);
      } catch (cause) {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : '导演 API 当前不可用');
          setNotice('运行时离线');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void initialize();
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  useEffect(() => {
    if (!project) return;
    const source = new EventSource(`/v1/projects/${project.id}/events/stream?follow=true`);
    source.onopen = () => setNotice('实时事件流已连接');
    source.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as EventRecord;
        setEvents((current) => (current.some((item) => item.id === event.id) ? current : current.concat(event)));
      } catch {
        // REST 刷新仍是最终可信来源。
      }
    };
    source.onerror = () => setNotice('实时事件流正在重连…');
    const timer = window.setInterval(() => {
      void refresh(project.id);
    }, 4000);
    return () => {
      source.close();
      window.clearInterval(timer);
    };
  }, [project?.id, refresh]);

  const shots = project && project.plans.length ? project.plans[project.plans.length - 1].shots : [];
  const active = shots[selected] || shots[0];
  const latest = useMemo(() => {
    const map = new Map<string, Attempt>();
    for (const attempt of project?.attempts || []) {
      const current = map.get(attempt.shot_id);
      if (!current || attempt.number > current.number) map.set(attempt.shot_id, attempt);
    }
    return map;
  }, [project?.attempts]);
  const passCount = shots.filter((shot) => latest.get(shot.id)?.judge_result?.verdict === 'PASS').length;
  const criteriaCount = shots.reduce((sum, shot) => sum + shot.acceptance_criteria.length, 0);
  const passedCriteria = shots.reduce(
    (sum, shot) => sum + (latest.get(shot.id)?.judge_result?.criterion_results || []).filter((item) => item.verdict === 'PASS').length,
    0,
  );
  const unresolved = project?.clarification_turns.filter((turn) => !turn.confirmed) || [];
  const phase = phaseFor(project?.status);

  const focusSection = (section: NavSection) => {
    setActiveNav(section);
    const target = section === 'projects' ? 'shot-queue' : section === 'review' ? (unresolved.length ? 'clarification-gate' : 'review-gate') : 'control-room';
    window.requestAnimationFrame(() => document.getElementById(target)?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  };

  const submitAnswers = async () => {
    if (!project) return;
    setRunning(true);
    try {
      await apiFetch<Project>(`/v1/projects/${project.id}/clarifications`, {
        method: 'POST',
        body: JSON.stringify(answers),
      });
      await refresh(project.id);
      setNotice('澄清信息已确认。准备好后即可生成计划。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '澄清信息保存失败');
    } finally {
      setRunning(false);
    }
  };

  const runQualityLoop = async () => {
    if (!project) return;
    setRunning(true);
    setNotice('Agent 正在执行质量闭环…');
    try {
      let next = project;
      if (!next.plans.length) next = await apiFetch<Project>(`/v1/projects/${next.id}/plan`, { method: 'POST' });
      next = await apiFetch<Project>(`/v1/projects/${next.id}/run`, {
        method: 'POST',
        body: JSON.stringify({ approve_plan: true, actor: 'operator' }),
      });
      await refresh(next.id);
      setNotice('视频已完成组装，等待你审批交付。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '质量闭环执行失败');
      await refresh(project.id).catch(() => undefined);
    } finally {
      setRunning(false);
    }
  };

  const retryShot = async () => {
    if (!project || !active) return;
    setRunning(true);
    try {
      await apiFetch<Project>(`/v1/projects/${project.id}/shots/${active.id}/retry`, {
        method: 'POST',
        body: JSON.stringify({ actor: 'operator' }),
      });
      await refresh(project.id);
      setNotice(`镜头 ${active.sequence} 已完成重试。`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '当前无法重试镜头');
    } finally {
      setRunning(false);
    }
  };

  const deliver = async () => {
    if (!project) return;
    setRunning(true);
    try {
      await apiFetch<Project>(`/v1/projects/${project.id}/deliver`, {
        method: 'POST',
        body: JSON.stringify({ actor: 'operator' }),
      });
      await refresh(project.id);
      setNotice('交付已批准，最终视频已就绪。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '交付审批失败');
    } finally {
      setRunning(false);
    }
  };

  if (loading) {
    return (
      <div className="loading-screen" role="status" aria-live="polite">
        <LoaderCircle size={20} className="spin" aria-hidden="true" />
        正在读取导演状态
      </div>
    );
  }

  return (
    <>
      <a className="skip-link" href="#control-room">跳转到主要内容</a>
      <div className="app-shell">
        <aside className="sidebar">
          <div className="brand">
            <div className="brand-mark"><Film size={18} aria-hidden="true" /></div>
            <div><strong>Director</strong><span>AI 视频制作系统</span></div>
          </div>
          <nav aria-label="主导航">
            <button className={`nav-item ${activeNav === 'control' ? 'active' : ''}`} aria-current={activeNav === 'control' ? 'page' : undefined} onClick={() => focusSection('control')}>
              <Activity size={16} aria-hidden="true" />控制台
            </button>
            <button className={`nav-item ${activeNav === 'projects' ? 'active' : ''}`} aria-current={activeNav === 'projects' ? 'page' : undefined} onClick={() => focusSection('projects')}>
              <GitBranch size={16} aria-hidden="true" />项目
            </button>
            <button className={`nav-item ${activeNav === 'review' ? 'active' : ''}`} aria-current={activeNav === 'review' ? 'page' : undefined} onClick={() => focusSection('review')}>
              <Inbox size={16} aria-hidden="true" />待审核
              <b aria-label={`${unresolved.length + (project?.status === 'awaiting_human' ? 1 : 0)} 项待审核`}>
                {unresolved.length + (project?.status === 'awaiting_human' ? 1 : 0)}
              </b>
            </button>
          </nav>
          <div className="sidebar-foot">
            <div className="provider"><span className="pulse" aria-hidden="true" />运行时已连接</div>
            <small>Provider 适配器 · Mock / Fal 风格 / ComfyUI</small>
          </div>
        </aside>

        <main className="main" id="control-room" tabIndex={-1}>
          <header className="topbar">
            <div>
              <p className="eyebrow">项目 / {(project?.name || 'DIRECTOR').toUpperCase()}</p>
              <h1>制作控制台</h1>
            </div>
            <div className="top-actions">
              <span className="status-chip"><span className="dot" aria-hidden="true" />{statusLabel(project?.status)}</span>
              <button className="icon-button" aria-label="刷新项目状态" title="刷新项目状态" onClick={() => project && refresh(project.id)}>
                <Activity size={18} aria-hidden="true" />
              </button>
              <div className="avatar" aria-label="操作员资料">OP</div>
            </div>
          </header>

          <section className="phase-bar" aria-label="制作阶段">
            {phases.map((name, index) => (
              <div key={name} className={`phase ${index < phase ? 'done ' : ''}${index === phase ? 'current' : ''}`} aria-current={index === phase ? 'step' : undefined}>
                <span aria-hidden="true">{index < phase ? <Check size={13} aria-hidden="true" /> : index + 1}</span>
                {name}
                {index < phases.length - 1 && <ArrowRight size={13} aria-hidden="true" />}
              </div>
            ))}
          </section>

          <section className={`notice ${error ? 'notice-error' : ''}`} role={error ? 'alert' : 'status'} aria-live={error ? 'assertive' : 'polite'}>
            <Sparkles size={16} aria-hidden="true" />
            <span>{error || notice}</span>
            <button onClick={() => project && refresh(project.id)}><RotateCcw size={15} aria-hidden="true" />刷新状态</button>
          </section>

          {unresolved.length > 0 && (
            <section className="panel clarify-panel" id="clarification-gate" tabIndex={-1}>
              <div className="panel-head">
                <div><p className="eyebrow">需求澄清</p><h2>请补充缺失的创作信息</h2></div>
                <span className="gate-count">{unresolved.length} 项待确认</span>
              </div>
              <div className="clarify-list">
                {unresolved.map((turn) => (
                  <label className="clarify-row" key={turn.id}>
                    <span>{turn.question}</span>
                    <input id={`clarification-${turn.id}`} aria-label={turn.question} value={answers[turn.id] || ''} placeholder="填写你的答案" onChange={(event) => setAnswers((current) => ({ ...current, [turn.id]: event.target.value }))} />
                  </label>
                ))}
              </div>
              <button className="primary clarify-submit" onClick={submitAnswers} disabled={running || unresolved.some((turn) => !answers[turn.id]?.trim())}>
                <Check size={15} aria-hidden="true" />确认答案
              </button>
            </section>
          )}

          <section className="metrics" aria-label="项目指标">
            <Metric icon={<Film size={17} aria-hidden="true" />} label="镜头" value={shots.length ? `${passCount} / ${shots.length}` : '—'} detail={shots.length ? '已通过' : '尚未规划'} />
            <Metric icon={<ShieldCheck size={17} aria-hidden="true" />} label="验收" value={criteriaCount ? `${passedCriteria} / ${criteriaCount}` : '—'} detail="项通过" />
            <Metric icon={<WalletCards size={17} aria-hidden="true" />} label="费用" value={`$${(project?.total_cost_usd || 0).toFixed(2)}`} detail={`预算 $${(project?.brief.budget_usd || 0).toFixed(0)}`} />
            <Metric icon={<Gauge size={17} aria-hidden="true" />} label="生成尝试" value={String(project?.attempts.length || 0)} detail="条溯源记录" />
          </section>

          <div className="work-grid">
            <section className="panel shots-panel" id="shot-queue" tabIndex={-1}>
              <div className="panel-head">
                <div><p className="eyebrow">执行队列</p><h2>镜头队列</h2></div>
                <button className="primary" onClick={runQualityLoop} disabled={running || unresolved.length > 0 || project?.status === 'delivered'}>
                  <Play size={15} aria-hidden="true" />{running ? '执行中…' : shots.length ? '运行质量闭环' : '生成计划'}
                </button>
              </div>
              {shots.length ? (
                <div className="shot-list">
                  {shots.map((shot, index) => {
                    const verdict = latest.get(shot.id)?.judge_result?.verdict;
                    return (
                      <button key={shot.id} className={`shot-row ${selected === index ? 'selected' : ''}`} aria-pressed={selected === index} onClick={() => setSelected(index)}>
                        <span className="shot-number">{String(shot.sequence).padStart(2, '0')}</span>
                        <span className="shot-copy"><strong>{shot.title}</strong><small>{shot.description}</small></span>
                        <span className={`result ${verdict === 'PASS' ? 'pass' : verdict === 'FAIL' ? 'fail' : 'queued'}`}>
                          {verdict === 'PASS' ? <Check size={14} aria-hidden="true" /> : verdict === 'FAIL' ? <CircleAlert size={14} aria-hidden="true" /> : <span aria-hidden="true" />}
                          {verdictLabel(verdict || 'QUEUED')}
                        </span>
                        <ArrowRight size={15} className="row-arrow" aria-hidden="true" />
                      </button>
                    );
                  })}
                </div>
              ) : (
                <div className="empty-state"><CircleAlert size={18} aria-hidden="true" />确认澄清信息后，系统会创建镜头计划。</div>
              )}
            </section>

            <section className="panel detail-panel">
              {active ? (
                <>
                  <div className="panel-head">
                    <div><p className="eyebrow">镜头 {String(active.sequence).padStart(2, '0')} / 验收标准</p><h2>{active.title}</h2></div>
                    <span className="score">{verdictLabel(latest.get(active.id)?.judge_result?.verdict || 'PENDING')}<small>{latest.get(active.id)?.number ? ` · 第 ${latest.get(active.id)?.number} 次尝试` : ''}</small></span>
                  </div>
                  <p className="description">{active.description}</p>
                  <div className="criteria">
                    {active.acceptance_criteria.map((criterion) => {
                      const result = latest.get(active.id)?.judge_result?.criterion_results?.find((item) => item.criterion_id === criterion.id);
                      const passed = result?.verdict === 'PASS';
                      const failed = result?.verdict === 'FAIL';
                      return (
                        <div className="criterion" key={criterion.id}>
                          <span className={`criterion-icon ${passed ? 'passed' : failed ? 'failed' : ''}`} aria-hidden="true">{passed ? <Check size={13} aria-hidden="true" /> : <CircleAlert size={13} aria-hidden="true" />}</span>
                          <div>
                            <strong>{criterionLabel(criterion.category)}</strong>
                            <p>{criterion.statement}</p>
                            {failed && <small className="failure-detail">{failureCodeLabel(result.failure_code)} · {reasonLabel(result.reason)}</small>}
                          </div>
                          <span className={`criterion-state ${passed ? 'passed' : failed ? 'failed' : 'pending'}`}>{verdictLabel(result?.verdict || 'PENDING')}</span>
                        </div>
                      );
                    })}
                  </div>
                  <div className="prompt-block">
                    <div className="prompt-head"><span>提示词包 v{active.prompt_bundle?.version || 1}</span><button onClick={retryShot} disabled={running}><RotateCcw size={14} aria-hidden="true" />重试镜头</button></div>
                    <code>{active.prompt_bundle?.positive || '计划获批后将自动生成提示词。'}</code>
                  </div>
                </>
              ) : (
                <div className="empty-state"><CircleAlert size={18} aria-hidden="true" />暂未选择镜头。</div>
              )}
            </section>
          </div>

          <section className="bottom-grid">
            <div className="panel timeline-panel">
              <div className="panel-head">
                <div><p className="eyebrow">审计轨迹</p><h2>Agent 决策</h2></div>
                <button className="quiet" onClick={() => project && refresh(project.id)}>刷新事件流 <ArrowRight size={14} aria-hidden="true" /></button>
              </div>
              <div className="events">{events.slice(-6).reverse().map((event) => <Event key={event.id} event={event} />)}{events.length === 0 && <div className="empty-state">暂时没有事件记录。</div>}</div>
            </div>
            <div className="panel gate-panel" id="review-gate" tabIndex={-1}>
              <p className="eyebrow">下一个人工门禁</p>
              <h2>{project?.status === 'awaiting_human' ? '审批最终组装' : project?.status === 'delivered' ? '交付已完成' : '当前无需人工处理'}</h2>
              <p>{project?.artifacts.length ? project.artifacts[project.artifacts.length - 1].uri : '所有镜头通过后，组装产物会显示在这里。'}</p>
              <button className="primary wide" onClick={deliver} disabled={running || project?.status !== 'awaiting_human' || !project?.artifacts.length}><ShieldCheck size={15} aria-hidden="true" />批准交付</button>
            </div>
          </section>
        </main>
      </div>
    </>
  );
}

function phaseFor(status?: string): number {
  if (status === 'clarifying') return 0;
  if (status === 'awaiting_plan_approval') return 1;
  if (status === 'planned' || status === 'generating') return 2;
  if (status === 'judging') return 3;
  if (status === 'repairing') return 4;
  return 5;
}

function statusLabel(status?: string): string {
  const labels: Record<string, string> = {
    clarifying: '等待需求澄清',
    awaiting_plan_approval: '等待计划审批',
    planned: '计划已就绪',
    generating: '正在生成',
    judging: '正在验收',
    repairing: '正在修复',
    awaiting_human: '等待人工审批',
    assembling: '正在组装',
    delivered: '已交付',
    failed: '失败',
    cancelled: '已取消',
  };
  return labels[status || ''] || '运行时离线';
}

function verdictLabel(verdict: string): string {
  return ({ PASS: '通过', FAIL: '失败', PENDING: '待验收', QUEUED: '排队中' } as Record<string, string>)[verdict] || verdict;
}

function criterionLabel(category: string): string {
  return ({ subject: '主体', action: '动作', camera: '镜头', continuity: '连续性', style: '风格', technical: '技术', audio: '音频', safety: '安全' } as Record<string, string>)[category.toLowerCase()] || category;
}

function failureCodeLabel(code?: string): string {
  if (!code) return '验收失败';
  return ({
    mock_quality_failure: '模拟产物质量不达标',
    missing_evidence: '缺少验收证据',
    judge_error: 'Judge 返回错误',
    low_confidence: '判定置信度过低',
    unknown_criterion: '未知验收项',
    provider_error: 'Provider 调用失败',
  } as Record<string, string>)[code] || code;
}

function reasonLabel(reason?: string): string {
  if (!reason) return 'Judge 建议修复';
  return ({
    'Mock artifact intentionally fails the first attempt': '模拟产物在首次尝试中按预期失败',
    'Judge requested repair': 'Judge 建议修复',
  } as Record<string, string>)[reason] || reason;
}

function Metric({ icon, label, value, detail }: { icon: ReactNode; label: string; value: string; detail: string }) {
  return <div className="metric"><span className="metric-icon">{icon}</span><div><small>{label}</small><strong>{value}</strong><em>{detail}</em></div></div>;
}

function Event({ event }: { event: EventRecord }) {
  const warning = event.event_type.includes('failed') || event.event_type.includes('repair') || event.event_type.includes('hard_stop');
  return <div className="event"><span className={`event-icon ${warning ? 'warn' : ''}`} aria-hidden="true">{event.event_type.includes('judge') ? <ShieldCheck size={14} /> : warning ? <CircleAlert size={14} /> : <Sparkles size={14} />}</span><div><strong>{eventLabel(event.event_type)}</strong><p>{eventSummary(event)}</p></div><time>{formatTime(event.created_at)}</time></div>;
}

function eventLabel(type: string): string {
  const labels: Record<string, string> = {
    'project.created': '项目已创建',
    'clarification.answered': '需求澄清已提交',
    'plan.created': '计划已生成',
    'plan.approved': '计划已审批',
    'plan.rolled_back': '计划已回滚',
    'project.paused': '项目已暂停',
    'generation.started': '开始生成',
    'budget.hard_stop': '已触发预算硬停止',
    'budget.soft_warning': '预算软警告',
    'continuity.failed': '连续性检查失败',
    'scheduler.blocked': '调度被阻塞',
    'scheduler.interrupted': '调度已中断',
    'scheduler.failed': '调度失败',
    'scheduler.awaiting_human': '等待人工处理',
    'scheduler.incomplete': '调度未完成',
    'scheduler.wave.started': '执行批次已开始',
    'scheduler.shot.persisted': '镜头状态已保存',
    'parallel.worker.started': '并行 Worker 已启动',
    'shot.generation.requested': '已请求生成镜头',
    'shot.generation.ready': '镜头产物已就绪',
    'project.delivered': '项目已交付',
    'project.cancelled': '项目已取消',
    'provider.callback.stale': '已忽略过期回调',
    'provider.callback.invalid': 'Provider 回调无效',
    'provider.callback': 'Provider 回调已接收',
    'shot.attempt.created': '已创建生成尝试',
    'shot.attempt.resumed': '已恢复生成尝试',
    'shot.provider_failed': 'Provider 生成失败',
    'shot.submitted.persisted': '外部任务已保存',
    'shot.generated': '镜头已生成',
    'shot.judged': '镜头验收完成',
    'shot.repair_planned': '已规划镜头修复',
    'shot.generated.resumed': '镜头生成结果已恢复',
    'shot.cost.reconciled': '镜头成本已对账',
    'shot.judged.resumed': '镜头验收结果已恢复',
    'shot.split': '镜头已拆分',
    'shot.retry_requested': '已请求镜头重试',
    'assembly.failed': '组装失败',
    'assembly.ready': '组装产物已就绪',
    'audio.generated': '音频产物已生成',
  };
  return labels[type] || type;
}

function eventSummary(event: EventRecord): string {
  const payload = event.payload;
  if (payload.verdict) return `验收结果：${verdictLabel(String(payload.verdict))}`;
  if (payload.kind) return `修复方式：${repairKindLabel(String(payload.kind))}`;
  if (payload.remaining !== undefined) return `${String(payload.remaining)} 项澄清信息待确认`;
  if (payload.cost_usd !== undefined) return `费用：$${Number(payload.cost_usd).toFixed(2)}`;
  if (payload.error) return `错误：${String(payload.error)}`;
  if (payload.reason) return `原因：${String(payload.reason)}`;
  return '项目状态已保存';
}

function repairKindLabel(kind: string): string {
  return ({ prompt: '调整提示词', parameters: '调整参数', reference: '替换 Reference', provider: '切换 Provider', split_shot: '拆分镜头', human: '请求人工处理' } as Record<string, string>)[kind.toLowerCase()] || kind;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}
