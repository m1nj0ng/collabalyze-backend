from flask import Flask, request, redirect, jsonify
from flask_sqlalchemy import SQLAlchemy
import os
import requests
from dotenv import load_dotenv
from github import Github
from celery import Celery
from celery.result import AsyncResult
import lizard
import time
import calendar

# 환경 변수 로드
load_dotenv()

app = Flask(__name__)

# ==========================================
# Flask 기본 JSON 응답 설정 변경 (추가!)
# ==========================================
app.json.ensure_ascii = False  # 한글이 \uXXXX 로 깨지는 현상 방지
app.json.compact = False       # 자동으로 들여쓰기(Pretty Print) 적용

# ==========================================
# 1. 데이터베이스 설정
# ==========================================
basedir = os.path.abspath(os.path.dirname(__file__))
# PostgreSQL 연결 설정
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


# SQLAlchemy 객체 초기화
db = SQLAlchemy(app)

# ==========================================
# 1.5. Celery (비동기 작업) 환경 설정
# ==========================================
# Redis를 Message Broker 및 Result Backend로 설정
app.config['CELERY_BROKER_URL'] = 'redis://localhost:6379/0'
app.config['CELERY_RESULT_BACKEND'] = 'redis://localhost:6379/0'
app.config['CELERY_TRACK_STARTED'] = True # 작업 시작 시 PENDING -> STARTED로 상태 변경

def make_celery(app):
    """Flask 애플리케이션 컨텍스트를 지원하는 Celery 인스턴스 생성"""
    celery = Celery(
        app.import_name,
        backend=app.config['CELERY_RESULT_BACKEND'],
        broker=app.config['CELERY_BROKER_URL']
    )
    celery.conf.update(app.config)

    # 백그라운드 Task가 Flask의 DB 연결 등 Context를 사용할 수 있도록 래핑
    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery

# Celery 객체 초기화
celery = make_celery(app)

# GitHub OAuth 인증 키 로드
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")

# GitHub API 데이터 수집용 토큰
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") 

# ==========================================
# 2. 데이터베이스 모델 (Schema) 설계
# ==========================================

# 테이블 1: 사용자 (User) - GitHub OAuth 연동 정보 저장
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    github_id = db.Column(db.String(100), unique=True, nullable=False) # GitHub 사용자명
    access_token = db.Column(db.String(200), nullable=True)            # GitHub API 접근용 Access Token
    profile_image = db.Column(db.String(200), nullable=True)           # 대시보드 표시용 프로필 이미지 URL
    created_at = db.Column(db.DateTime, default=db.func.now())

    # ContributionData 테이블과의 1:N 관계 설정
    contributions = db.relationship('ContributionData', backref='user', lazy=True)

# 테이블 2: 프로젝트 (Project) - 분석 대상 Repository 정보 저장
class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    repo_url = db.Column(db.String(200), unique=True, nullable=False)  # GitHub Repository URL
    name = db.Column(db.String(100), nullable=False)                   # Repository 이름
    created_at = db.Column(db.DateTime, default=db.func.now())

    # ContributionData 테이블과의 1:N 관계 설정
    contributions = db.relationship('ContributionData', backref='project', lazy=True)

# 테이블 3: 기여도 데이터 (ContributionData) - 협업 분석을 위한 지표 저장
class ContributionData(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    
    # 양적 및 질적 평가 지표
    commits = db.Column(db.Integer, default=0)
    loc_added = db.Column(db.Integer, default=0)
    loc_deleted = db.Column(db.Integer, default=0)
    pull_requests = db.Column(db.Integer, default=0)
    issues = db.Column(db.Integer, default=0)
    code_reviews = db.Column(db.Integer, default=0)
    
    collected_at = db.Column(db.DateTime, default=db.func.now())

# 테이블 4: 커밋 상세 데이터 (CommitDetail) - AI 문맥 분석용 Deep Data
class CommitDetail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    
    # [1. 깃허브 원본 데이터]
    commit_hash = db.Column(db.String(100), unique=True, nullable=False) # 커밋 고유 해시값 (중복 수집 방지용)
    message = db.Column(db.Text, nullable=False)                         # 커밋 메시지 원본 (AI 문맥 분석용 재료)
    loc_added = db.Column(db.Integer, default=0)                         # 추가된 코드 라인 수
    loc_deleted = db.Column(db.Integer, default=0)                       # 삭제된 코드 라인 수
    
    # [2. 백엔드 정적 분석 데이터]
    complexity_score = db.Column(db.Float, nullable=True)                # 사이클로매틱 복잡도 점수
    committed_at = db.Column(db.DateTime, nullable=False)                # 실제 깃허브에 커밋된 날짜와 시간

    diff_text = db.Column(db.Text, nullable=True)                        # 코드 변경 사항 원본 텍스트(Diff)

# 테이블 5: PR 상세 데이터 (PullRequestDetail) - AI 문맥 분석용 Deep Data
class PullRequestDetail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    
    pr_number = db.Column(db.Integer, nullable=False)                    # PR 번호 (#1, #2 등)
    title = db.Column(db.String(500), nullable=False)                    # PR 제목
    body = db.Column(db.Text, nullable=True)                             # PR 본문 내용
    comments = db.Column(db.Text, nullable=True)                         # PR 댓글 및 코드리뷰 대화 내역
    state = db.Column(db.String(20), nullable=False)                     # 상태 (open, closed 등)
    created_at = db.Column(db.DateTime, nullable=False)                  # 작성일

# 테이블 6: 이슈 상세 데이터 (IssueDetail) - AI 문맥 분석용 Deep Data
class IssueDetail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    
    issue_number = db.Column(db.Integer, nullable=False)                 # 이슈 번호 (#1, #2 등)
    title = db.Column(db.String(500), nullable=False)                    # 이슈 제목
    body = db.Column(db.Text, nullable=True)                             # 이슈 본문 내용 
    comments = db.Column(db.Text, nullable=True)                         # 이슈 대화 내역
    state = db.Column(db.String(20), nullable=False)                     # 상태 (open, closed 등)
    created_at = db.Column(db.DateTime, nullable=False)                  # 작성일

# ==========================================
# 3. GitHub OAuth (소셜 로그인) API 라우터
# ==========================================

@app.route('/api/auth/github')
def github_login():
    # 1. 클라이언트를 GitHub 인증 페이지로 리다이렉트
    # scope=repo: Private Repository 접근 권한 요청
    github_auth_url = f"https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}&scope=repo"
    return redirect(github_auth_url)

@app.route('/api/auth/github/callback')
def github_callback():
    # 2. GitHub 인증 완료 후 반환된 임시 인증 코드(code) 수신
    code = request.args.get('code')
    if not code:
        return jsonify({"error": "로그인 취소 및 인증 코드 누락"}), 400

    # 3. 수신한 인증 코드를 사용하여 GitHub Access Token 발급 요청
    token_response = requests.post(
        'https://github.com/login/oauth/access_token',
        data={
            'client_id': GITHUB_CLIENT_ID,
            'client_secret': GITHUB_CLIENT_SECRET,
            'code': code
        },
        headers={'Accept': 'application/json'}
    )
    access_token = token_response.json().get('access_token')

    # 4. 발급받은 Access Token을 사용하여 사용자 프로필 정보 조회
    user_response = requests.get(
        'https://api.github.com/user',
        headers={'Authorization': f'token {access_token}'}
    )
    user_info = user_response.json()
    github_id = user_info.get('login')
    profile_image = user_info.get('avatar_url')

    # 5. 데이터베이스에 사용자 정보 저장 (기존 사용자인 경우 토큰 및 프로필 업데이트)
    user = User.query.filter_by(github_id=github_id).first()
    if not user:
        user = User(github_id=github_id, access_token=access_token, profile_image=profile_image)
        db.session.add(user)
    else:
        user.access_token = access_token
        user.profile_image = profile_image
        
    # 트랜잭션 커밋
    db.session.commit() 

    # 6. 로그인 성공 결과 반환
    return jsonify({
        "status": "success", 
        "message": f"환영합니다, {github_id}님!", 
        "user_id": user.id
    })

# ==========================================
# 4. 프로젝트 (Repository) 등록 API 라우터
# ==========================================

@app.route('/api/projects', methods=['POST'])
def register_project():
    # 1. 클라이언트(프론트엔드)로부터 JSON 데이터 수신
    data = request.get_json()
    repo_url = data.get('repo_url')

    if not repo_url:
        return jsonify({"error": "repo_url 데이터가 누락되었습니다."}), 400

    # 2. URL에서 레포지토리 이름(owner/repo) 추출 
    # 예: https://github.com/m1nj0ng/Collabalyze -> m1nj0ng/Collabalyze
    name = repo_url.replace("https://github.com/", "").replace(".git", "")

    # 3. 데이터베이스 조회하여 중복 등록 방지
    project = Project.query.filter_by(repo_url=repo_url).first()
    
    # 4. 신규 프로젝트인 경우 DB에 저장
    if not project:
        project = Project(repo_url=repo_url, name=name)
        db.session.add(project)
        db.session.commit()
        message = "프로젝트가 성공적으로 등록되었습니다."
    else:
        message = "이미 등록된 프로젝트입니다."

    # 5. 등록 결과 반환
    return jsonify({
        "status": "success",
        "message": message,
        "project_id": project.id,
        "project_name": project.name
    })

# ==========================================
# 403 Rate Limit 방어 함수
# ==========================================
def enforce_rate_limit(g):
    """현재 깃허브 API 잔여량을 확인하고, 한도 임박 시 대기하는 로직"""
    # g.get_rate_limit().core 대신, 마지막 API 호출 헤더에 남은 잔여량을 직접 가져옴
    remaining, limit = g.rate_limiting
    
    if remaining < 50:
        # g.rate_limiting_resettime은 리셋 시간을 초(timestamp) 단위로 바로 뱉어줌
        reset_timestamp = g.rate_limiting_resettime
        current_timestamp = time.time()
        sleep_time = max(0, reset_timestamp - current_timestamp) + 10
        
        if sleep_time > 0:
            print(f"[경고] API 호출 한도 임박 (남은 횟수: {remaining}). {int(sleep_time)}초 대기합니다.")
            time.sleep(sleep_time)
            print("[안내] 대기 완료. 수집을 재개합니다.")

# ==========================================
# 5. 프로젝트 데이터 수집 비동기 Task (@celery.task)
# ==========================================
@celery.task(bind=True)
def collect_project_data_task(self, project_id):
    """
    백그라운드에서 GitHub API와 통신하여 데이터를 수집하고 DB에 저장하는 함수
    """
    project = Project.query.get(project_id)
    if not project:
        return {"error": "해당 프로젝트를 찾을 수 없습니다."}

    g = Github(GITHUB_TOKEN)
    
    try:
        repo = g.get_repo(project.name)
        contributors = repo.get_contributors()
        
        collected_count = 0
        
        # 기여자별 데이터 수집 및 DB 저장
        for contributor in contributors:
            github_id = contributor.login
            
            # 1. 유저 정보 확인 및 저장 
            user = User.query.filter_by(github_id=github_id).first()
            if not user:
                user = User(github_id=github_id, profile_image=contributor.avatar_url)
                db.session.add(user)
                db.session.flush() 
            
            # ==========================================
            # 2. 커밋 텍스트 및 코드 변경량(Diff) 상세 수집 (Lizard 분석 적용)
            # ==========================================
            commits = repo.get_commits(author=contributor)
            
            commit_count = 0
            total_loc_added = 0    # 유저의 총 추가 라인 누적 변수
            total_loc_deleted = 0  # 유저의 총 삭제 라인 누적 변수

            # API 호출 낭비를 막기 위해 아예 분석할 필요가 없는 파일 확장자 목록
            IGNORE_EXTENSIONS = ('.md', '.txt', '.png', '.jpg', '.jpeg', '.gif', '.json', '.csv', '.yml', '.yaml')
            
            for c in commits:
                commit_count += 1
                
                # [API 한도확인 1] 커밋 100개를 처리할 때마다 API 한도를 확인합니다.
                if commit_count % 100 == 0:
                    enforce_rate_limit(g)
                
                # DB 중복 검사 전에 깃허브에서 라인 수부터 무조건 가져와서 누적하기!
                additions = c.stats.additions if c.stats else 0
                deletions = c.stats.deletions if c.stats else 0
                
                total_loc_added += additions
                total_loc_deleted += deletions
                
                # 중복 방지: 이미 DB에 저장된 커밋인지 해시값(sha)으로 확인
                existing_commit = CommitDetail.query.filter_by(commit_hash=c.sha).first()
                
                if not existing_commit:
                    # 다국어 파일 필터링 및 Lizard 복잡도 계산
                    total_complexity = 0

                    # Diff 텍스트를 모을 리스트
                    diff_texts_list = []
                    
                    for file in c.files:
                        # Diff 텍스트 수집: patch(diff) 데이터가 존재한다면 리스트에 담기
                        if file.patch: 
                            diff_texts_list.append(f"--- {file.filename} ---\n{file.patch}")
                            
                        # 1. 무시할 확장자가 아니고, 삭제된 파일이 아닌 경우에만 진행
                        if not file.filename.lower().endswith(IGNORE_EXTENSIONS) and file.status != 'removed':
                            try:
                                # 2. 깃허브 API를 찔러서 해당 시점(c.sha)의 파일 원본 코드를 가져옴
                                file_content = repo.get_contents(file.filename, ref=c.sha).decoded_content.decode('utf-8')
                                
                                # 3. Lizard에 코드 원본을 통과시켜서 분석 결과 추출
                                analysis = lizard.analyze_file.analyze_source_code(file.filename, file_content)
                                
                                # 4. 파일 내 모든 함수/메서드의 사이클로매틱 복잡도 점수 합산
                                file_complexity = sum([func.cyclomatic_complexity for func in analysis.function_list])
                                total_complexity += file_complexity
                            except Exception as e:
                                # 코드가 깨져있거나(바이너리 파일 등) 파싱에 실패하면 무시하고 다음 파일로 넘어감
                                pass

                    # 모은 Diff 텍스트들을 하나의 텍스트로 합침
                    final_diff_text = "\n\n".join(diff_texts_list)

                    new_commit = CommitDetail(
                        user_id=user.id,
                        project_id=project.id,
                        commit_hash=c.sha,
                        message=c.commit.message,
                        loc_added=additions,
                        loc_deleted=deletions,
                        complexity_score=total_complexity,
                        diff_text=final_diff_text,
                        committed_at=c.commit.author.date
                    )
                    db.session.add(new_commit)
            
            # ==========================================
            # 3. PR 텍스트 + 댓글/리뷰 코멘트 수집
            # ==========================================
            pr_query = f"repo:{project.name} is:pr author:{github_id}"
            prs = g.search_issues(pr_query)
            pr_count = 0
            
            # [API 한도확인 2] PR 수집 반복문을 돌기 직전에 한도를 확인합니다.
            enforce_rate_limit(g)
            
            for pr in prs:
                pr_count += 1
                # 중복 방지 (pr_number와 project_id로 확인)
                existing_pr = PullRequestDetail.query.filter_by(pr_number=pr.number, project_id=project.id).first()
                
                if not existing_pr:
                    # [댓글 수집 로직]
                    comments_list = []
                    try:
                        # 1. 일반 PR 댓글
                        for comment in pr.get_comments():
                            comments_list.append(f"[{comment.user.login}]: {comment.body}")
                            
                        # 2. 코드 라인에 남긴 진짜 '코드 리뷰' 댓글
                        pr_obj = repo.get_pull(pr.number)
                        for rev_comment in pr_obj.get_review_comments():
                            comments_list.append(f"[Code Review - {rev_comment.user.login}]: {rev_comment.body}")
                    except:
                        pass
                        
                    comments_text = "\n".join(comments_list) # 댓글들을 엔터 단위로 하나의 문자열로 합침

                    new_pr = PullRequestDetail(
                        user_id=user.id,
                        project_id=project.id,
                        pr_number=pr.number,
                        title=pr.title,
                        body=pr.body if pr.body else "",
                        comments=comments_text,
                        state=pr.state,
                        created_at=pr.created_at,
                    )
                    db.session.add(new_pr)

            # ==========================================
            # 4. 이슈 상세 텍스트 수집
            # ==========================================
            issue_query = f"repo:{project.name} type:issue author:{github_id}"
            issues = g.search_issues(issue_query)
            issue_count = 0
            
            # [API 한도확인 3] 이슈 수집 반복문을 돌기 직전에 한도를 확인합니다.
            enforce_rate_limit(g)
            
            for issue in issues:
                issue_count += 1
                # 중복 방지 (issue_number와 project_id로 확인)
                existing_issue = IssueDetail.query.filter_by(issue_number=issue.number, project_id=project.id).first()
                
                if not existing_issue:
                    # [댓글 수집 로직]
                    comments_list = []
                    try:
                        for comment in issue.get_comments():
                            comments_list.append(f"[{comment.user.login}]: {comment.body}")
                    except:
                        pass
                        
                    comments_text = "\n".join(comments_list)

                    new_issue = IssueDetail(
                        user_id=user.id,
                        project_id=project.id,
                        issue_number=issue.number,
                        title=issue.title,
                        body=issue.body if issue.body else "", 
                        comments=comments_text,
                        state=issue.state,
                        created_at=issue.created_at
                    )
                    db.session.add(new_issue)
            
            # 5. 통계 데이터(ContributionData) 업데이트 로직 
            review_query = f"repo:{project.name} type:pr reviewed-by:{github_id}"
            review_count = g.search_issues(review_query).totalCount

            contribution = ContributionData.query.filter_by(user_id=user.id, project_id=project.id).first()
            
            if not contribution:
                contribution = ContributionData(
                    user_id=user.id,
                    project_id=project.id,
                    commits=commit_count,  
                    pull_requests=pr_count,
                    issues=issue_count,
                    code_reviews=review_count,       # 찾은 리뷰 횟수 저장
                    loc_added=total_loc_added,       # 누적된 추가 라인 저장
                    loc_deleted=total_loc_deleted    # 누적된 삭제 라인 저장
                )
                db.session.add(contribution)
            else:
                contribution.commits = commit_count
                contribution.pull_requests = pr_count
                contribution.issues = issue_count
                contribution.code_reviews = review_count       # 찾은 리뷰 횟수 업데이트
                contribution.loc_added = total_loc_added       # 누적된 추가 라인 업데이트
                contribution.loc_deleted = total_loc_deleted   # 누적된 삭제 라인 업데이트
                contribution.collected_at = db.func.now()
                
            collected_count += 1
            
        # 모든 반복문이 끝나고 DB에 한 번에 반영
        db.session.commit()
        return {"status": "success", "collected_count": collected_count, "project_name": project.name}

    except Exception as e:
        db.session.rollback()
        return {"error": str(e)}

# ==========================================
# 5.1. 프로젝트 데이터 수집 요청 API 라우터 (POST)
# ==========================================
@app.route('/api/projects/<int:project_id>/collect', methods=['POST'])
def collect_project_data(project_id):
    """
    수집 요청을 받아 Celery Task를 호출하고 즉시 응답을 반환하는 라우터
    """
    project = Project.query.get(project_id)
    if not project:
        return jsonify({"error": "해당 프로젝트를 찾을 수 없습니다."}), 404

    # .delay()를 사용하여 백그라운드 Task 호출
    task = collect_project_data_task.delay(project_id)
    
    return jsonify({
        "status": "processing",
        "message": "데이터 수집이 백그라운드에서 시작되었습니다.",
        "project_name": project.name,
        "task_id": task.id
    }), 202
    
# ==========================================
# 5.2. 데이터 수집 작업 상태 확인 API 라우터 (GET)
# ==========================================
@app.route('/api/projects/tasks/<task_id>', methods=['GET'])
def get_task_status(task_id):
    """
    클라이언트가 전달받은 task_id를 통해 백그라운드 작업의 현재 상태를 조회하는 API
    """
    task = AsyncResult(task_id, app=celery)

    if task.state == 'PENDING':
        response = {
            "state": task.state,
            "message": "작업이 큐에 대기 중입니다."
        }
    elif task.state == 'STARTED':
        response = {
            "state": task.state,
            "message": "데이터 수집 작업을 진행 중입니다."
        }
    elif task.state == 'SUCCESS':
        response = {
            "state": task.state,
            "result": task.result  
        }
    elif task.state == 'FAILURE':
        response = {
            "state": task.state,
            "error": str(task.info)  
        }
    else:
        response = {
            "state": task.state,
            "message": f"현재 상태: {task.state}"
        }

    return jsonify(response)

# ==========================================
# 6. 프로젝트 기여도 데이터 조회 API (GET) - 시간 및 상태 정보 포함 버전
# ==========================================
@app.route('/api/projects/<int:project_id>/contributions', methods=['GET'])
def get_project_contributions(project_id):
    project = Project.query.get(project_id)
    if not project:
        return jsonify({"error": "해당 프로젝트를 찾을 수 없습니다."}), 404

    contributions = ContributionData.query.filter_by(project_id=project.id).order_by(ContributionData.commits.desc()).all()
    
    result = []
    for c in contributions:
        user_id = c.user_id
        
        # [데이터 1] 커밋 내역 (메시지 + 날짜)
        # diff_text는 백엔드 분석용이므로 여기서는 제외하고 AI 팀원용 데이터만 구성
        commits = CommitDetail.query.filter_by(user_id=user_id, project_id=project.id).all()
        commit_data_list = []
        for commit in commits:
            commit_data_list.append({
                "message": commit.message if commit.message else "",
                "date": commit.committed_at.strftime("%Y-%m-%d %H:%M:%S") if commit.committed_at else None
            })
        
        total_complexity = sum([commit.complexity_score for commit in commits if commit.complexity_score is not None])
        
        # [데이터 2] PR 내역 (제목, 본문, 댓글, 상태, 날짜)
        prs = PullRequestDetail.query.filter_by(user_id=user_id, project_id=project.id).all()
        pr_data_list = []
        for pr in prs:
            pr_data_list.append({
                "title": pr.title,
                "body": pr.body if pr.body else "",
                "comments": pr.comments.split('\n') if pr.comments else [],
                "state": pr.state,
                "date": pr.created_at.strftime("%Y-%m-%d %H:%M:%S") if pr.created_at else None
            })

        # [데이터 3] 이슈 내역 (제목, 본문, 댓글, 상태, 날짜)
        issues = IssueDetail.query.filter_by(user_id=user_id, project_id=project.id).all()
        issue_data_list = []
        for issue in issues:
            issue_data_list.append({
                "title": issue.title,
                "body": issue.body if issue.body else "",
                "comments": issue.comments.split('\n') if issue.comments else [],
                "state": issue.state,
                "date": issue.created_at.strftime("%Y-%m-%d %H:%M:%S") if issue.created_at else None
            })

        user_data = {
            "username": c.user.github_id,
            "profile_image": c.user.profile_image,
            
            "1_quantitative_data": {
                "commits": c.commits,
                "pull_requests": c.pull_requests,
                "issues": c.issues,
                "code_reviews": c.code_reviews,
                "loc_added": c.loc_added,
                "loc_deleted": c.loc_deleted
            },
            
            "2_nlp_data": {
                "commits": commit_data_list,  # 'commit_messages' 대신 구조화된 'commits' 사용
                "pull_requests": pr_data_list, 
                "issues": issue_data_list       
            },
            
            "3_static_code_analysis_data": {
                "total_complexity_score": total_complexity,
                "backend_code_score": None
            }
        }
        result.append(user_data)

    return jsonify({
        "status": "success",
        "project_name": project.name,
        "total_contributors": len(result),
        "data": result
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)