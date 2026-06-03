import requests, http.cookiejar
jar = http.cookiejar.MozillaCookieJar(r"C:\Users\Lucian\Desktop\Biomodels sources\cookies.txt")
jar.load(ignore_discard=True, ignore_expires=True)
s = requests.Session()
s.cookies.update(jar)
s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
r = s.get("https://research.ebsco.com/linkprocessor/v2-pdf?sourceRecordId=nkcvb4uaiv&recordId=nkcvb4uaiv&profileIdentifier=2onyl7&intent=view&type=pdfLink&lang=en-US", allow_redirects=True)
print(r.status_code, r.headers.get("content-type"), len(r.content), r.content[:4])