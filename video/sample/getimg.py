import sys,json,urllib.request,urllib.parse
UA={"User-Agent":"VeganVideoBot/0.1 (research)"}
credits=[]
for title,name in [("File:Capitol Building Full View.jpg","capitol"),("File:City County Building Denver Colorado 2024.jpg","denver"),("File:Domestic duck with her ducklings.jpg","duck"),("File:Wooden gavel 20170417.jpg","gavel"),("File:Ballot drop box 2020.jpg","ballot")]:
    u="https://commons.wikimedia.org/w/api.php?"+urllib.parse.urlencode({"action":"query","format":"json","titles":title,"prop":"imageinfo","iiprop":"url|extmetadata","iiurlwidth":1600})
    p=list(json.load(urllib.request.urlopen(urllib.request.Request(u,headers=UA)))["query"]["pages"].values())[0]["imageinfo"][0]
    data=urllib.request.urlopen(urllib.request.Request(p["thumburl"],headers=UA)).read()
    open(f"img/{name}.jpg","wb").write(data)
    m=p["extmetadata"]; credits.append(f'{name}: {title} — {m.get("Artist",{}).get("value","?")[:60]} — {m.get("LicenseShortName",{}).get("value")}')
    print(name,len(data))
open("CREDITS.txt","w",encoding="utf8").write("\n".join(credits))
