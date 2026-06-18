from dci_policy_collector import HttpClient

client = HttpClient()
resp = client.get("https://gall.dcinside.com/mgallery/board/lists/?id=jaetae&page=500&exception_mode=recommend")
print(resp.url)  # 실제 리다이렉트된 URL
print(resp.text[:500])  # 응답 내용 앞부분