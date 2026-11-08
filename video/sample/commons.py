import sys,json,urllib.request,urllib.parse
q=sys.argv[1]; out=sys.argv[2]
u="https://commons.wikimedia.org/w/api.php?"+urllib.parse.urlencode({"action":"query","format":"json","generator":"search","gsrsearch":q+" filetype:bitmap","gsrnamespace":6,"gsrlimit":6,"prop":"imageinfo","iiprop":"url|extmetadata|size","iiurlwidth":1600})
r=json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"VeganVideoBot/0.1 (research)"})))
for p in sorted(r.get("query",{}).get("pages",{}).values(),key=lambda p:p["index"]):
    ii=p["imageinfo"][0]; m=ii["extmetadata"]
    print(p["index"],p["title"],ii["width"],ii["height"],m.get("LicenseShortName",{}).get("value"),"|",ii["thumburl"])
