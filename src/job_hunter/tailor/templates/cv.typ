// ATS-friendly one-column CV. Data comes from cv.json written next to this file.
#let cv = json("cv.json")
#let L = cv.labels

#set document(title: cv.name + " — CV", author: cv.name)
#set page(paper: "a4", margin: (x: 1.6cm, y: 1.4cm))
#set text(font: ("Libertinus Serif", "New Computer Modern"), size: 10pt, lang: cv.lang)
#set par(justify: false, leading: 0.55em)

#let section(title) = {
  v(0.6em)
  text(size: 11.5pt, weight: "bold", upper(title))
  v(-0.5em)
  line(length: 100%, stroke: 0.5pt)
  v(-0.2em)
}

#align(center)[
  #text(size: 18pt, weight: "bold", cv.name) \
  #text(size: 11pt, cv.headline) \
  #let c = cv.contact
  #let parts = ("email", "phone", "location", "linkedin", "github").filter(k => k in c and c.at(k) != "" and c.at(k) != none).map(k => c.at(k))
  #text(size: 9pt, parts.join("  |  "))
]

#section(L.summary)
#cv.summary

#section(L.experience)
#for e in cv.experience [
  #grid(columns: (1fr, auto), [*#e.role* — #e.company], align(right, e.dates))
  #if e.location != "" [#text(size: 9pt, style: "italic", e.location)]
  #for b in e.bullets [
    - #b
  ]
  #v(0.25em)
]

#section(L.skills)
#for s in cv.skills [
  *#s.group:* #s.items.join(", ") \
]

#if cv.education.len() > 0 [
  #section(L.education)
  #for ed in cv.education [
    #grid(columns: (1fr, auto), [*#ed.degree* — #ed.school], ed.year)
  ]
]

#if cv.languages.len() > 0 [
  #section(L.languages)
  #cv.languages.join(" · ")
]

#for s in cv.at("sections", default: ()) [
  #section(s.title)
  #for i in s.items [
    - #i
  ]
]
