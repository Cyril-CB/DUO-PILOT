"""Exercise real local Git patches and journal states; remote services are doubles."""
import difflib
import json
from pathlib import Path
import subprocess

import pytest

from duopilot import executor, kanban, settings
from duopilot.db import get_db
from duopilot.execution_io import ExecutionError, Workspace
from duopilot.providers import AgentReply, ProviderError
from test_v4 import app, accepted_task

ORIGINAL = 'def add(a, b):\n    return a - b\n'
FIXED = 'def add(a, b):\n    return a + b\n'
FINAL = 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n'


def patch(before=ORIGINAL,after=FIXED):
    return 'diff --git a/app.py b/app.py\n'+''.join(difflib.unified_diff(
        before.splitlines(keepends=True),after.splitlines(keepends=True),fromfile='a/app.py',tofile='b/app.py'))


def develop(value=None):
    return {'message':'Correction fondée sur le fichier lu.','status':'patch','patch':value or patch(),'read_requests':[]}


def review(verdict='approve'):
    return {'message':'Revue du diff et des tests fournis.','verdict':verdict,'read_requests':[],'findings':[]}


def inspect_head(workspace):
    return subprocess.check_output(['git','rev-parse','HEAD'],cwd=workspace.folder,text=True).strip()


class Agent:
    def __init__(self,replies,hook=None):
        self.replies,self.hook,self.calls = iter(replies),hook,[]
    def generate(self,request,timeout,tick):
        self.calls.append(request)
        if self.hook:
            self.hook(request)
        reply = next(self.replies)
        if isinstance(reply,Exception):
            raise reply
        return AgentReply(text=reply['message'],payload=reply,model='test-double',input_tokens=1,output_tokens=1)


class Services:
    def __init__(self):
        self.heads=[]
        self.pr=None
        self.pushes=[]
        self.published=[]
        self.test_results=[]
        self.test_calls=[]
        self.fail_publish=False
        self.fail_finish=False
        self.during_tests=None
        self.workspaces=[]

    def workspace(self,folder,job,config,budget):
        services = self
        class Local(Workspace):
            def prepare(self):
                self.folder.mkdir(parents=True,exist_ok=True)
                if not (self.folder/'.git').exists():
                    self.git(['init'])
                    self.git(['checkout','-b',self.job['branch']])
                    (self.folder/'app.py').write_text(ORIGINAL)
                    (self.folder/'README.md').write_text('Fixture locale avec un calcul à corriger.\n')
                    self.git(['add','app.py','README.md'])
                    self.git(['commit','-m','Initial fixture'])
                return self.head()
            def push(self,sha):
                assert sha==self.head() and self.clean()
                services.pushes.append(sha)
                if services.pr:
                    services.pr['head']['sha']=sha
            def finish_patch(self,intent):
                result = super().finish_patch(intent)
                if services.fail_finish:
                    services.fail_finish=False
                    raise ExecutionError('Interruption simulée après le commit.')
                return result
        workspace = Local(folder,job,config,budget)
        self.workspaces.append(workspace)
        return workspace

    def tests(self,config,budget):
        services = self
        class Tests:
            def resolve_image(self,image):
                return 'sha256:'+'a'*64
            def run(self,workspace,image,argv):
                budget.tick()
                passed = services.test_results.pop(0) if services.test_results else True
                services.test_calls.append(workspace.head())
                if services.during_tests:
                    services.during_tests()
                return {'passed':passed,'exit_code':0 if passed else 1,'output':'Résultat simulé.',
                        'image':image,'command':argv,'sha':workspace.head()}
        return Tests()

    def github(self,repo,config,budget):
        services = self
        class Remote:
            def open_heads(self):
                return services.heads+([services.pr['head']['ref']] if services.pr and services.pr['state']=='open' else [])
            def find(self,branch):
                return services.pr
            def publish(self,job,sha,body):
                services.published.append(body)
                services.pr = {'number':17,'state':'open','merged':False,
                    'head':{'ref':job['branch'],'sha':sha},'base':{'ref':job['base_branch']}}
                if services.fail_publish:
                    services.fail_publish=False
                    raise ExecutionError('Réponse perdue après la création distante.')
                return services.pr
            def request(self,method,endpoint):
                assert method=='GET' and endpoint=='pulls/17'
                return services.pr
            def feedback(self,number):
                return [{'body':'Ajouter une explication au calcul.'}]
        return Remote()

    def run(self,job,agent=None):
        return executor.run_execution(job,agent=agent,workspace_factory=self.workspace,
            test_factory=self.tests,github_factory=self.github)


def queued(split=False):
    task = accepted_task(split=split)
    if split:
        task=kanban.task_detail(task['children'][0]['id'])
    job,created = executor.enqueue_task(task['id'])
    assert created
    return task,job


def test_complete_pipeline_uses_both_roles_and_same_tested_reviewed_commit(app):
    with app.app_context():
        task,job = queued()
        services,agent = Services(),Agent([develop(),review()])
        assert services.run(job,agent)
        result = executor.detail(job)
        assert result['status']=='pr_open',result['error']
        assert result['attempts']==1 and result['calls']==2
        assert result['approved_sha']==result['tested_sha']==result['published_sha']==services.pushes[0]
        assert [r['actor'] for r in agent.calls]==[task['developer'],task['reviewer']]
        assert agent.calls[1]['tests']['passed'] and 'return a + b' in agent.calls[1]['diff']
        assert result['base_branch']=='dev' and result['branch'].startswith('duo/task-')
        assert kanban.task_detail(task['id'])['status']=='review'
        assert not services.run(job,Agent([]))
        assert len(services.pushes)==1
        assert (services.workspaces[0].folder/'app.py').read_text()==FIXED


@pytest.mark.parametrize('failed_tests',[True,False])
def test_failed_test_or_review_requires_new_patch_tests_and_review(app,failed_tests):
    with app.app_context():
        _,job = queued()
        services = Services()
        services.test_results=[not failed_tests,True]
        agent = Agent([develop(),review('approve' if failed_tests else 'changes_requested'),
                       develop(patch(FIXED,FINAL)),review()])
        services.run(job,agent)
        result = executor.detail(job)
        assert result['status']=='pr_open',result['error']
        assert len(services.test_calls)==2 and len(services.pushes)==1
        assert services.test_calls[0]!=services.test_calls[1]==result['published_sha']
        assert result['attempts']==2 and result['calls']==4
        assert agent.calls[2]['tests']['passed']==(not failed_tests)


def test_failed_cli_is_charged_and_increased_limits_allow_explicit_resume(app):
    with app.app_context():
        settings.save(settings.DEFAULTS|{'EXECUTION_ENABLED':True,'EXECUTION_ATTEMPTS':1},0)
        _,job = queued()
        services = Services()
        services.run(job,Agent([ProviderError('Quota de test épuisé.')]))
        result = executor.detail(job)
        assert result['status']=='blocked' and result['attempts']==1 and result['calls']==1
        assert any(e['status']=='failed' for e in result['events'])
        with pytest.raises(ExecutionError,match='Budget'):
            executor.resume(job)
        settings.save(settings.DEFAULTS|{'EXECUTION_ENABLED':True,'EXECUTION_ATTEMPTS':2},1)
        executor.resume(job)
        services.run(job,Agent([develop(),review()]))
        assert executor.detail(job)['status']=='pr_open'
        assert executor.detail(job)['attempts']==2


def test_read_request_consumes_attempt_and_provides_source_next_call(app):
    with app.app_context():
        _,job = queued()
        agent = Agent([{'message':'Lire le calcul.','status':'read','patch':'','read_requests':[{'path':'app.py','start_line':1}]},develop(),review()])
        services = Services()
        services.run(job,agent)
        assert executor.detail(job)['status']=='pr_open'
        assert executor.detail(job)['attempts']==2
        assert 'return a - b' in agent.calls[1]['code']['excerpts'][0]['content']


def test_pause_and_duration_stop_before_another_model_call(app):
    with app.app_context():
        _,job = queued()
        services = Services()
        agent = Agent([develop()],hook=lambda _:executor.pause(job))
        services.run(job,agent)
        first=executor.detail(job)
        assert first['status']=='paused' and not services.pushes
        assert first['attempts']==1
        executor.resume(job)
        executor._update(job,elapsed_seconds=1800)
        never=Agent([])
        services.run(job,never)
        assert not never.calls
        assert 'durée' in executor.detail(job)['error']
        assert executor.detail(job)['attempts']==1


def test_commit_checkpoint_and_pr_response_loss_resume_without_redeveloping(app):
    with app.app_context():
        _,job=queued()
        services=Services()
        services.fail_finish=True
        services.run(job,Agent([develop()]))
        assert executor.detail(job)['phase']=='apply'
        committed=inspect_head(services.workspaces[-1])
        executor.resume(job)
        services.fail_publish=True
        services.run(job,Agent([review()]))
        result=executor.detail(job)
        assert result['phase']=='publish' and result['status']=='blocked'
        assert result['published_sha']==committed
        executor.resume(job)
        services.run(job)  # no CLI constructed or called while resuming publication
        result=executor.detail(job)
        assert result['status']=='pr_open' and result['pr_number']==17
        assert result['attempts']==1 and result['calls']==2
        assert inspect_head(services.workspaces[-1])==committed


def test_capacity_stops_before_any_model_or_test(app):
    with app.app_context():
        _,job=queued()
        services=Services()
        services.heads=['one','two','three']
        agent=Agent([])
        services.run(job,agent)
        result=executor.detail(job)
        assert result['status']=='blocked' and 'Trois' in result['error']
        assert not agent.calls and not services.test_calls


def test_user_edit_during_work_stops_without_overwriting_card(app):
    with app.app_context():
        task,job=queued()
        def edit(_):
            card=kanban.task_detail(task['id'])
            kanban.add_comment(task['id'],card['version'],'Demande précisée pendant le travail.')
        services=Services()
        services.run(job,Agent([develop()],hook=edit))
        result=executor.detail(job)
        assert result['status']=='blocked' and 'carte a changé' in result['error']
        assert not services.pushes
        assert kanban.task_detail(task['id'])['events'][0]['data']['text'].startswith('Demande précisée')


def test_merge_sync_unblocks_dependent_child_and_completes_parent(app):
    with app.app_context():
        task,job=queued(split=True)
        services=Services()
        services.run(job,Agent([develop(),review()]))
        parent=kanban.task_detail(task['parent_id'])
        other=parent['children'][1]['id']
        with pytest.raises(ExecutionError):
            executor.enqueue_task(other)
        services.pr.update(merged=True,state='closed')
        executor.sync_pr(job,github_factory=services.github)
        assert kanban.task_detail(task['id'])['status']=='done'
        job2,_=executor.enqueue_task(other)
        second=Services()
        second.run(job2,Agent([develop(),review()]))
        second.pr.update(merged=True,state='closed')
        executor.sync_pr(job2,github_factory=second.github)
        assert kanban.task_detail(parent['id'])['status']=='done'
        before=kanban.task_detail(parent['id'])['version']
        executor.sync_pr(job2,github_factory=second.github)
        assert kanban.task_detail(parent['id'])['version']==before


def test_feedback_rework_keeps_branch_and_cumulative_budget(app):
    with app.app_context():
        _,job=queued()
        services=Services()
        services.run(job,Agent([develop(),review()]))
        before=executor.detail(job)
        executor.sync_pr(job,github_factory=services.github,feedback=True)
        executor.resume(job)
        with pytest.raises(ExecutionError,match='Attendre'):
            executor.sync_pr(job,github_factory=services.github)
        agent=Agent([develop(patch(FIXED,FINAL)),review()])
        services.run(job,agent)
        result=executor.detail(job)
        assert result['status']=='pr_open',result['error']
        assert result['branch']==before['branch'] and result['attempts']==2
        assert result['elapsed_seconds']>=before['elapsed_seconds']
        assert agent.calls[0]['github_feedback'][0]['body'].startswith('Ajouter')


def test_dirty_code_before_publication_cannot_be_pushed(app):
    with app.app_context():
        _,job=queued()
        services=Services()
        def tamper(request):
            if request['stage']=='review_code':
                (services.workspaces[-1].folder/'app.py').write_text('Unexpected change\n')
        services.run(job,Agent([develop(),review()],hook=tamper))
        assert executor.detail(job)['status']=='blocked'
        assert not services.pushes


def test_merge_does_not_erase_a_new_human_request_on_the_card(app):
    with app.app_context():
        task,job=queued()
        services=Services()
        services.run(job,Agent([develop(),review()]))
        card=kanban.task_detail(task['id'])
        kanban.add_comment(task['id'],card['version'],'Un cas supplémentaire doit encore être traité.')
        services.pr.update(merged=True,state='closed')
        executor.sync_pr(job,github_factory=services.github)
        assert executor.detail(job)['status']=='merged'
        assert kanban.task_detail(task['id'])['status']=='review'


def test_only_one_worker_claims_a_job_and_one_job_per_repository(app):
    with app.app_context():
        task,job=queued(split=True)
        first_owner=executor._claim(job)
        assert first_owner and executor._claim(job) is None
        db=get_db()
        parent=kanban.task_detail(task['parent_id'])
        other=parent['children'][1]['id']
        with db:
            db.execute('UPDATE tasks SET depends_on=NULL WHERE id=?',(other,))
        second,_=executor.enqueue_task(other)
        assert executor._claim(second) is None
        executor._update(job,status='paused')
        assert executor._claim(second)
