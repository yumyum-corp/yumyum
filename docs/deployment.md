# 배포 설정 및 실행 준비

현재는 배포 계획 1단계의 설정을 준비한 상태다. Dockerfile과 Compose 실행 구성은 후속 단계에서 추가한다.

## 환경변수 준비

루트 `.env.example`을 `.env`로 복사하고 빈 값을 채운다. 실제 비밀값은 Git에 커밋하지 않는다. 예시의 `example.com`은 실제 도메인으로 교체한다.

- MySQL의 일반 계정은 `DB_USERNAME`/`DB_PASSWORD`, 관리 계정은 `MYSQL_ROOT_PASSWORD`를 사용한다. 앱은 root 계정을 사용하지 않는다.
- Compose 작성 시 `MYSQL_USER=${DB_USERNAME:?DB_USERNAME is required}`, `MYSQL_PASSWORD=${DB_PASSWORD:?DB_PASSWORD is required}`, `MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD is required}`로 연결한다.
- Spring에는 `SPRING_PROFILES_ACTIVE=prod`와 해당 서비스 변수만 전달한다. AI에 루트 `.env` 전체를 마운트하지 않는다. AI Settings는 모르는 dotenv 항목을 거부할 수 있다.
- Compose의 `.env`는 자동으로 컨테이너에 전달되지 않는다. 서비스별 `environment`에 명시해야 한다.
- Compose 필수 검증 대상은 `DB_USERNAME`, `DB_PASSWORD`, `MYSQL_ROOT_PASSWORD`, `DB_URL`, `JWT_SECRET`, `REDIS_HOST`, `AI_FASTAPI_URL`, `FOOD_API_KEY`, 카카오 client ID/secret 및 redirect/CORS 값이다. `${NAME:?NAME is required}`로 누락과 빈 값을 모두 차단한다. 실제 적용 및 실패 검증은 Compose 작성 단계에서 수행한다.
- JWT secret은 UTF-8 기준 최소 32바이트의 충분히 무작위인 값을 사용한다. 배포 profile에는 개발용 fallback이 없다.
- `ENV=dev`는 AI mock 모드다. 실제 AI 사용 시 `ENV=prod`와 유효한 `GMS_API_KEY`가 필요하다. 식품 API는 별도이므로 AI mock 모드가 Spring의 식품 초기 로딩까지 mock 처리하지 않는다.
- `FOOD_API_KEY`는 Spring 식품 API, `MFDS_API_KEY`는 AI 식품 API용이다. 각 서버의 실제 호출에 맞는 키를 설정한다.
- `VITE_API_BASE_URL=/api`는 frontend 빌드 시 사용한다. 브라우저에 공개되며 실행 중 환경변수만 변경해서는 빌드 결과가 바뀌지 않는다.

## 서비스 주소와 로그인

| 연결 | 설정 |
|---|---|
| 브라우저 → Spring | `/api/*` → `http://backend:8080/api/*` |
| Spring → AI | `AI_FASTAPI_URL=http://ai:8000` |
| Spring → MySQL | `DB_URL=jdbc:mysql://mysql:3306/yumyum?...` |
| Spring → Redis | `REDIS_HOST=redis`, `REDIS_PORT=6379` |
| AI → Spring | `SPRING_BASE_URL=http://backend:8080`는 현재 미사용 예약 값 |

Spring의 context path는 `/api`다. Nginx는 이 접두사를 제거하지 않고 전달해야 한다. AI의 Spring 호출은 현재 없으므로 경로를 임의로 추가하지 않는다. 추후 호출을 구현할 때 `/api`를 정확히 한 번 포함한다.

카카오 로그인 시작 주소는 `https://<도메인>/api/oauth2/authorization/kakao`, 등록할 redirect URI는 `https://<도메인>/api/login/oauth2/code/kakao`다. `KAKAO_REDIRECT_URI`와 카카오 개발자 콘솔의 값을 일치시킨다. 로그인 성공 후 `FRONTEND_REDIRECT_URL=https://<도메인>/oauth/callback`으로 이동한다. CORS에는 경로 없는 origin을 설정한다.

배포 profile은 forwarded 헤더를 처리한다. backend 포트는 외부에 공개하지 않고, Nginx는 클라이언트가 보낸 `Forwarded`를 제거하고 `X-Forwarded-*` 헤더를 신뢰할 수 있는 값으로 덮어써야 한다. HTTPS 기준 세션 쿠키 설정을 사용하므로 카카오 로그인 전체 검증은 HTTPS 구성 후 진행한다. 현재 액세스·리프레시 토큰은 OAuth callback 쿼리로 전달되어 frontend localStorage에 저장된다. 위 세션 쿠키 설정은 토큰 저장 방식을 변경하지 않는다. 프록시 access log에는 토큰이 포함된 callback 쿼리를 남기지 않도록 설정한다.

## 초기 DB와 스키마 관리

현재 Flyway/Liquibase migration 및 별도 SQL seed는 없다. 개발 profile은 기존 `ddl-auto=update`를 유지한다. 배포 profile 기본값은 `validate`로, 빈 DB에는 바로 기동할 수 없다.

첫 번째 폐기 가능한 데모 DB는 다음 순서로 준비한다.

1. MySQL이 `MYSQL_DATABASE=yumyum` 및 일반 앱 계정을 생성하도록 Compose를 구성한다. JDBC URL의 DB 이름도 `yumyum`으로 맞춘다.
2. `.env`의 `DB_DDL_AUTO=update`를 명시적으로 설정한 뒤 backend를 기동해 엔티티 기반 스키마를 생성한다. 실제 사용자 데이터가 있는 DB에서는 이 초기화 절차를 사용하지 않는다.
3. 기동 및 테이블 생성 확인 후 `DB_DDL_AUTO=validate`로 돌리고 backend 컨테이너를 재생성한다. 단순 restart는 변경된 환경변수를 적용하지 않는다.
4. 다시 정상 기동되는지 확인하고 생성된 스키마를 백업한다.
5. 실제 사용자 데이터를 저장하는 배포 전에는 확정된 스키마를 초기 migration으로 관리하고, 이후 변경은 버전별 migration으로 적용한다. migration 도구와 초기 SQL 구현은 배포 계획 6단계에서 수행한다.

MySQL 초기 환경변수는 새 데이터 디렉터리 초기화 때만 적용된다. 기존 볼륨의 계정·비밀번호는 `.env` 수정만으로 변경되지 않는다.

`FoodDataInitializer`는 시작 시 음식 테이블 건수를 확인하고 0건이면 `FoodBulkLoadService`를 통해 식품 API에서 데이터를 로딩한다. 따라서 유효한 `FOOD_API_KEY`와 외부 통신이 필요하다. 음식 검색 검증 전 로딩 로그와 데이터 건수를 확인한다. 일부만 적재된 상태는 0건이 아니므로 재시작만으로 전체 재로딩되지 않는다. 임의로 테이블을 삭제하지 말고 누락 여부와 복구 방식을 확인한다.

로컬 더미 키로 기동하면 현재 설정된 식품 API가 `NO_OPENAPI_SERVICE_ERROR`(HTTP 400)를 반환할 수 있다. 이 오류는 앱 기동 실패와 별개지만, 운영 전 `FOOD_API_URL`의 서비스가 현재도 유효한지와 발급받은 키가 해당 서비스에 등록되어 있는지 확인한다. 필요하면 초기 대량 적재를 별도 작업으로 분리해 앱 시작을 외부 API 상태에 묶지 않는다.

회원·Program·Meal은 로그인·온보딩·기록 흐름으로 생성한다. 실제 데이터를 보존하려면 MySQL named volume과 별도 백업이 필요하며, 이 구성은 Compose 단계에서 추가한다.

## 다음 단계

서비스 Dockerfile과 `.dockerignore`를 준비한 뒤 Compose의 서비스별 변수 매핑, 필수값 검사, 볼륨 및 healthcheck를 구현한다. 그때 빈 값에서 `docker compose config --quiet`가 실패하는지와 올바른 설정으로 전체 서비스가 시작되는지 확인한다.
