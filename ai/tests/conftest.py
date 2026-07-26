import os

# app.config.settings는 최초 import 시 한 번만 생성되는 프로세스 전역 싱글턴이다.
# 개별 테스트 모듈의 os.environ.setdefault("ENV", "dev")는 어떤 모듈이 먼저
# 수집되어 app.config를 import하느냐에 따라 이미 늦을 수 있으므로,
# pytest가 테스트 모듈을 import하기 전인 conftest 로드 시점에 강제로 dev를 고정한다.
# (dev 모드가 아니면 실제 Claude/Vision API가 호출되어 비용이 발생한다.)
os.environ["ENV"] = "dev"
