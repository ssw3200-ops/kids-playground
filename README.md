# 🛝 오늘 놀이터 가도 될까?

우리 동네 미세먼지·날씨로 **지금 놀이터 가도 되는지, 오늘 몇 시가 좋은지, 어디로 갈지** 알려주는 페이지.
블로그 「아이랑 오늘 뭐하지?」 이웃 전용 (비밀번호).

- 페이지: https://ssw3200-ops.github.io/kids-playground/
- 매시간 GitHub Actions 가 공공데이터를 받아 `gh-pages` 브랜치로 배포 (데이터는 AES-GCM 암호화)
- 결과 확인: https://ssw3200-ops.github.io/kids-playground/status.json

## 처음 설정
1. Settings → Secrets and variables → Actions: `DATA_GO_KR_KEY`, `PAGE_PASSWORD`
2. Actions → 놀이터 날씨 자동 갱신 → Run workflow (첫 실행)
3. Settings → Pages → Deploy from a branch → `gh-pages` / `(root)`

## 판정 기준 (놀이터 기준)
| | 🟢 놀이터 GO | 🟡 짧게·조심 | 🔴 실내 추천 |
|---|---|---|---|
| 미세먼지 | 좋음·보통 | 초미세 26↑ 또는 미세 51↑ (보통 상단) | 나쁨 이상 |
| 비·눈 | 강수확률 30% 미만 | 30~59% / 비 그친 직후 | 비·눈 예보, 60% 이상 |
| 기온 | | 체감 0℃ 이하, 30℃ 이상 | 체감 -10℃ 이하, 33℃ 이상 |
| 바람 | | 6m/s 이상 | 9m/s 이상 |
| 자외선 | | 매우높음(8) 이상 | |
