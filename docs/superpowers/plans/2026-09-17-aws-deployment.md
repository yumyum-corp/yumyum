# Docker Compose 구성 및 AWS 첫 배포 계획

작성일: 2026-09-17
상태: 1단계 배포 설정 정리 완료. Compose 필수 변수 검증은 3단계에서 구현. AWS 리소스 생성 전.

## 1. 목표와 범위

로컬에서 Docker Compose로 전체 서비스를 실행한 뒤, 같은 구성을 AWS EC2 한 대에 배포한다. 최종 완료 기준은 HTTPS 주소에서 로그인 → 온보딩 → 식단 기록 → AI 연동 흐름이 동작하고, 컨테이너 재생성 후에도 데이터가 유지되는 것이다.

첫 배포는 frontend(Nginx), backend(Spring Boot), ai(FastAPI), MySQL, Redis를 한 EC2에서 실행한다. RDS 분리와 CI/CD는 첫 배포를 검증한 뒤 진행한다. 단일 EC2 장애 시 전체 서비스가 중단되는 구성이므로 데모 및 초기 배포를 목표로 한다.

## 2. 현재 저장소에서 확인한 사항

| 항목 | 현재 상태 | 계획에 반영할 내용 |
|---|---|---|
| 실제 폴더 | `backend/`, `frontend/`, `ai/` | 문서의 예시 경로 대신 실제 경로 사용 |
| Docker | `ai/Dockerfile`만 존재, 실행에 `--reload` 포함 | backend/frontend Dockerfile 및 Compose 추가, AI 배포 실행 수정 |
| Spring | Java 21, Spring Boot 3.5.14 | 실제 빌드 설정을 기준으로 이미지 구성 |
| Spring context path | `/api` | Nginx가 `/api`를 제거하지 않고 전달 |
| AI 연결 변수 | `AI_FASTAPI_URL` | `http://ai:8000` 주입 |
| DB 변수 | `DB_URL`, `DB_USERNAME`, `DB_PASSWORD` | Compose MySQL 서비스 주소 사용 |
| 스키마 관리 | `spring.jpa.hibernate.ddl-auto=update` | 초기 스키마 생성 방식과 배포용 마이그레이션 확정 |
| JWT | 로컬용 기본 secret 존재 | 배포에서는 별도 secret 필수 주입 |
| OAuth | 카카오 설정 및 프론트 redirect 설정 존재 | HTTPS 도메인과 callback URL 반영 |
| Vue | `VITE_API_BASE_URL=/api`, Vite 개발 프록시 존재 | 배포에서는 Nginx가 프록시 담당 |
| AI | GMS 설정 사용, `ENV=dev` 기본값 | 실제 코드의 GMS 환경변수 사용, mock 검증 후 실제 호출 검증 |
| AI health | `/health` 존재 | Compose healthcheck에 활용 |

AGENTS.md의 예시 환경변수와 현재 코드가 다르므로 현재 코드가 읽는 이름을 기준으로 배포한다. FastAPI의 `SPRING_BASE_URL`은 호출 코드의 경로 결합 방식을 확인한 뒤 `/api` 포함 여부를 결정한다.

## 3. 목표 구성

```text
브라우저
  └─ HTTPS :443 / HTTP :80
      └─ EC2: frontend 컨테이너의 Nginx
          ├─ /       → Vue 정적 파일
          └─ /api/*  → backend:8080/api/*
                         ├─ mysql:3306
                         ├─ redis:6379
                         └─ ai:8000 → 외부 AI API
```

- 호스트에 공개할 포트는 Nginx의 80/443만 사용한다.
- backend, ai, MySQL, Redis에는 호스트 `ports`를 설정하지 않는다.
- 내부 통신에는 Compose 서비스명을 사용한다. 컨테이너의 `localhost`를 다른 서비스 주소로 사용하지 않는다.
- AI 서버에는 외부 AI API 호출을 위한 outbound 통신이 필요하다.
- DB 쓰기와 스키마 관리는 Spring이 담당한다. AI에서 직접 DB 조회가 필요한 경우 읽기 전용 계정을 사용한다.

## 4. 단계별 작업

### 1단계 — 배포 설정 정리

- [x] 루트 `.env.example`에 서비스별 환경변수와 용도 작성
- [x] 필수 secret 목록과 Compose 검증 방식 확정 — 실제 구성·실패 검증은 3단계로 이동
- [x] `backend`의 배포용 profile 추가 및 local 기본값과 분리
- [x] `DB_URL`의 DB 이름과 MySQL 초기 생성 DB 이름을 `yumyum`으로 일치시키기
- [x] `AI_FASTAPI_URL=http://ai:8000`, `REDIS_HOST=redis` 설정
- [x] `SPRING_BASE_URL` 사용처 확인 — 현재 선언만 존재, 호출부 없음
- [x] 카카오 callback, 프론트 redirect, CORS, 프록시 전달 헤더 설정 확인
- [x] DB 초기 스키마 및 필수 초기 데이터 생성 절차 확정

산출물: 루트 `.env.example`, `backend/src/main/resources/application-prod.properties`, `docs/deployment.md`.
배포 기본값은 스키마 `validate`이며, 폐기 가능한 첫 데모 DB만 명시적으로 `update`를 사용해 초기화한다. 실제 데이터 저장 전 migration 구현은 6단계에서 수행한다. 현재 로그인은 카카오 OAuth이며 별도 비밀번호 회원가입 흐름을 가정하지 않는다.

주요 환경변수:

| 대상 | 변수 |
|---|---|
| MySQL | `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_ROOT_PASSWORD` |
| Spring | `DB_URL`, `DB_USERNAME`, `DB_PASSWORD`, `JWT_SECRET`, `REDIS_HOST`, `REDIS_PORT`, `AI_FASTAPI_URL`, `FOOD_API_KEY` |
| OAuth/브라우저 | `KAKAO_CLIENT_ID`, `KAKAO_CLIENT_SECRET`, `KAKAO_REDIRECT_URI`, `FRONTEND_REDIRECT_URL`, `CORS_ALLOWED_ORIGINS` |
| AI | `ENV`, `GMS_API_KEY`, `GMS_BASE_URL`, `ANTHROPIC_VERSION`, `DEFAULT_MODEL`, `MFDS_API_KEY`, `SPRING_BASE_URL` |
| Vue 빌드 | `VITE_API_BASE_URL=/api` |

Compose의 `.env`는 변수 치환용이므로 컨테이너별 `environment`에 필요한 값을 명시적으로 연결한다. `VITE_*`는 빌드 시 브라우저 코드에 포함되므로 비밀키를 넣지 않는다. 실제 `.env`는 Git과 이미지에 포함하지 않는다.

완료 기준: 각 서비스가 사용하는 환경변수와 초기 DB 준비 방식이 문서 및 설정에서 일치한다.

### 2단계 — 서비스 이미지 준비

- [x] `backend/Dockerfile`: Java 21 기반 다단계 빌드, Gradle Wrapper로 `bootJar` 생성, 실행 이미지에 필요한 JAR만 복사
- [x] Linux 빌드에서 Gradle Wrapper 실행 권한 설정
- [x] `frontend/Dockerfile`: lockfile 기반 `npm ci`, `npm run build`, Nginx 이미지에 `dist` 복사
- [x] frontend 빌드에 Node 22 Alpine 버전 고정
- [x] `ai/Dockerfile`: 배포 실행의 `--reload` 제거, Python 3.11 및 비root 실행 설정
- [x] 각 디렉터리에 `.dockerignore` 추가: `.env`, 로컬 빌드 결과, 가상환경, 캐시, 테스트 자료 제외
- [ ] 이미지 버전 태그와 배포 커밋 식별 규칙은 Compose/배포 단계에서 확정

검증 결과: Python `compileall`은 통과했다. Docker 빌드는 로컬 Docker 설정 디렉터리 접근 권한 오류로 실행하지 못했다. frontend는 로컬 `node_modules`가 없어 `vite`를 찾지 못했고, backend Gradle Wrapper는 네트워크 제한으로 Gradle 배포본을 다운로드하지 못했다. EC2 또는 권한이 정상인 Docker 환경에서 이미지 빌드 검증이 남아 있다.

### 3단계 — Nginx 및 Compose 작성

- [x] `nginx/nginx.conf` 작성: Vue 정적 파일과 SPA 새로고침 fallback 설정
- [x] `/api/` 요청을 URI 변경 없이 `backend:8080`에 전달
- [x] Host 및 forwarded 헤더 전달, Spring의 프록시 헤더 처리 설정 확인
- [x] AI 응답 시간을 고려한 프록시 timeout 설정
- [x] 루트 `compose.yml`에 `frontend`, `backend`, `ai`, `mysql`, `redis` 정의
- [x] 1단계에서 정리한 서비스별 환경변수 매핑 및 `${NAME:?message}` 필수값 검증 구현, 빈 값에서 config 검증 실패 확인
- [x] MySQL/Redis/AI/frontend healthcheck 구성; Spring은 별도 health endpoint가 없어 service_started 의존성 사용
- [x] MySQL/Redis/AI 준비 완료 후 backend가 시작하도록 의존성 설정
- [x] AI와 Spring이 서로를 기다리는 순환 시작 의존성 방지
- [x] 재시작 정책 설정
- [x] MySQL·Redis named volume 구성
- [x] Redis AOF로 재시작 시 refresh token 상태 보존 구성
- [x] Compose 및 MySQL의 시간대를 Asia/Seoul 기준으로 설정

산출물: `compose.yml`, `nginx/nginx.conf`, frontend build arg 설정.
검증 결과: 더미 환경변수로 `docker compose config --quiet` 통과. backend와 frontend 이미지는 빌드 성공. AI 이미지는 CPU 전용 PyTorch를 포함해 직접 빌드 성공. Nginx 설정은 `nginx -t` 통과(Compose 네트워크의 `backend` 이름을 임시 host로 매핑). 전체 Compose 기동은 MySQL 이미지 레지스트리 다운로드 중 일시적인 CloudFront EOF로 중단되어 재시도가 필요하다. healthcheck는 각 이미지에 실제 존재하는 `mysqladmin`, `redis-cli`, Python, `wget`을 사용한다.

### 4단계 — 로컬 통합 검증

- [x] `docker compose config --quiet`로 설정 검증
- [x] `docker compose up -d --build` 및 `docker compose ps`로 기동 확인
- [x] 필요한 서비스 로그에서 DB 연결 오류와 재시작 반복 여부 확인 — 앱은 정상 기동, 식품 초기 로딩은 잘못된 placeholder 키로 외부 API가 400 반환
- [x] Vue 진입 및 `/health` 확인
- [x] `/api/members/me`가 Nginx를 통해 Spring까지 전달되고 인증 없이 401 반환하는지 확인
- [ ] 회원가입·로그인·인증 갱신·로그아웃 확인 — 카카오 실제 자격증명 필요
- [ ] 온보딩 → Program 생성 → Meal 기록 → Streak 조회 확인 — 실제 로그인과 식품 API 키 필요
- [x] AI `ENV=dev`에서 API 키 없이 mock 모드 health 응답 확인
- [x] MySQL 재시작 후 스키마와 데이터베이스 접근 보존 확인
- [x] backend/AI/MySQL/Redis 포트가 호스트에 공개되지 않는지 확인
- [ ] MySQL 백업을 별도 테스트 DB에 복원하여 확인

실제 카카오 로그인 검증은 등록된 callback URL에서 수행한다. 현재 로컬 `.env`는 더미 값으로 생성되어 있으며 Git에서 무시된다. `FOOD_API_KEY=local-placeholder`는 의도적으로 유효하지 않아 음식 초기 로딩 로그에 `NO_OPENAPI_SERVICE_ERROR`가 남았다. AWS 전환 전 실제 식품안전처 서비스 키와 현재 endpoint의 유효성을 확인해야 한다. `docker compose down -v`는 데이터를 삭제하므로 일반 재배포 절차에 넣지 않는다.

완료 기준: 로컬 통합 흐름과 데이터 보존 검증을 통과한다.

### 5단계 — AWS 준비 및 EC2 배포

- [ ] AWS 계정 MFA와 예산 알림 설정
- [ ] 사용할 리전, 도메인, 월 예산 확정
- [ ] 로컬 메모리 사용량과 이미지 빌드 자원을 측정하여 EC2 사양 선택
- [ ] EC2 CPU 아키텍처와 이미지 아키텍처 일치 확인
- [ ] EC2 및 영구 데이터용 EBS 용량 결정, 인터넷 연결 가능한 서브넷에 배치
- [ ] 보안 그룹: 80/443 허용, SSH 사용 시 22는 관리자 IP로 제한
- [ ] Docker Engine 및 Compose 플러그인 설치, 재부팅 후 자동 실행 확인
- [ ] 고정 접속 주소가 필요하면 Elastic IP 할당 및 비용 확인
- [ ] 검증한 커밋의 코드 또는 동일 태그의 이미지 전달
- [ ] 서버 전용 `.env` 작성 및 접근 권한 제한
- [ ] 로컬과 같은 Compose로 기동, 정적 화면 및 health 확인
- [ ] CPU·메모리·디스크 사용량과 로그 증가량 확인

EC2, EBS, 공인 IPv4, 데이터 전송, 백업의 실제 비용은 생성 시점의 AWS 견적으로 확인한다. 처음에는 수동 배포로 절차를 검증하고, 서버 빌드 자원이 부족하면 CI에서 빌드한 이미지를 전달한다.

완료 기준: EC2에서 전체 서비스가 정상 실행되고 외부에서 Nginx에 접근할 수 있다.

### 6단계 — 도메인·HTTPS·실제 AI 연동

- [ ] 도메인 DNS를 EC2 접속 주소에 연결
- [ ] Nginx TLS 인증서 발급·마운트 및 자동 갱신 절차 구성
- [ ] 인증서 발급 전 HTTP 설정과 발급 후 HTTPS 설정의 적용 순서 문서화
- [ ] HTTP → HTTPS redirect 설정 및 인증서 갱신 경로 확인
- [ ] 카카오 개발자 설정과 `KAKAO_REDIRECT_URI`를 실제 HTTPS callback으로 일치시키기
- [ ] `FRONTEND_REDIRECT_URL`, `CORS_ALLOWED_ORIGINS`를 배포 origin으로 변경
- [ ] HTTPS 뒤의 OAuth URL 생성 및 쿠키를 사용한다면 Secure/SameSite 동작 확인
- [ ] `ENV=prod`와 유효한 GMS 설정으로 실제 AI 응답 확인
- [ ] 외부 AI API 및 식품 API가 EC2에서 호출 가능한지 확인
- [ ] 실제 데이터 저장 전 운영 스키마 관리 적용: 초기 migration 마련 후 `ddl-auto=validate` 등 검증 모드 사용
- [ ] HTTPS 주소에서 로그인부터 AI 응답까지 전체 흐름 재검증

완료 기준: 브라우저에 혼합 콘텐츠·인증 오류가 없고, 실제 AI 응답과 주요 기능이 동작한다.

### 7단계 — 재배포·백업·복구 절차 마무리

- [ ] 배포 커밋, 이미지 태그, 적용 시각 기록
- [ ] DB 백업을 EC2 외부 저장소에 보관하고 복원 절차 기록
- [ ] 이전 앱 이미지로 복귀하는 명령과 DB migration 호환성 확인
- [ ] 앱 롤백과 DB 복구는 별도 절차로 작성
- [ ] EC2 재부팅 후 서비스 및 데이터 복구 확인
- [ ] HTTPS 인증서 자동 갱신 검증
- [ ] 사용 종료 시 EC2뿐 아니라 EBS·Elastic IP·백업 등 잔여 리소스 정리 절차 작성

완료 기준: 다른 팀원도 문서만 보고 재배포 및 복구를 수행할 수 있다.

## 5. 예상 산출물

| 파일 | 용도 |
|---|---|
| `compose.yml` | 전체 서비스 실행 |
| `.env.example` | 배포 환경변수 안내 |
| `backend/Dockerfile`, `backend/.dockerignore` | Spring 이미지 빌드 |
| `frontend/Dockerfile`, `frontend/.dockerignore` | Vue 빌드 및 Nginx 실행 |
| `ai/Dockerfile`, `ai/.dockerignore` | AI 배포 이미지 |
| `nginx/nginx.conf` | 정적 파일, API 프록시 및 HTTPS 설정 |
| `backend/src/main/resources/application-prod.properties` | 배포 전용 설정 |
| `docs/deployment.md` | 로컬 실행·AWS 배포·HTTPS·백업·롤백 절차 |

인증서 갱신 설정과 DB migration 파일은 구현 방식 확정 시 추가한다.

## 6. 이후 확장

첫 배포가 안정화되면 MySQL을 RDS로 이전하고, 이미지 저장소 및 CI/CD를 도입한다. RDS 이전 시 같은 VPC의 비공개 DB로 구성하고 EC2 보안 그룹에서 오는 DB 연결만 허용한다. [AWS 공식 EC2–RDS 연결 안내](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/ec2-rds-connect.html)

ECS, 로드밸런서, Redis 관리형 서비스는 트래픽·가용성 요구와 비용을 확인한 뒤 검토한다.
