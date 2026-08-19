# Model Platform Landscape

A survey of the model catalogs that are actually useful for 3D printing, with
a popularity figure for each. It exists to justify which portals the plugin
registers and to record what changed in the ecosystem. It is a project note,
not an endorsement of any platform.

Traffic figures are Similarweb estimates for **July 2026**. They measure site
visits, not library size or model quality, and Similarweb does not publish an
error margin. Treat them as an ordering signal, not as exact numbers.

The popularity score is a 1-10 logarithmic scale derived from those visits:
34M+ maps to 10, ~15M to 9, ~10M to 8, ~5M to 7, ~2-4M to 6, ~1M to 5,
~600-900K to 4, ~200-500K to 3, under 100K to 2.

## Community 3D printing catalogs

| Score | Platform | Link | Visits/mo | Global rank | Library | Cost model | Registered |
|---|---|---|---|---|---|---|---|
| 10 | MakerWorld | https://makerworld.com | 34.8M | #986 | 2.6M models | Free only | Yes |
| 9 | Printables | https://www.printables.com | 17.2M | #2,485 | ~1.5M models | Free only | Yes |
| 9 | Cults3D | https://cults3d.com | 14.1M | #2,588 | ~3.2M models | Free and paid | Yes |
| 8 | Thingiverse | https://www.thingiverse.com | 11.7M | #3,449 | ~2.5M models | Free only | Yes |
| 7 | Yeggi | https://www.yeggi.com | 5.9M | #9,828 | 4.86M indexed | Meta-search | **No** |
| 6 | GrabCAD | https://grabcad.com/library | 3.4M | #11,968 | 5M CAD files | Free, membership | Yes |
| 6 | MyMiniFactory | https://www.myminifactory.com | 2.7M | #16,677 | Hundreds of thousands | Free, paid, Tribes | Yes |
| 6 | Creality Cloud | https://www.crealitycloud.com | 2.4M | #20,018 | 400K+ models | Free and paid | Yes |
| 5 | Thangs | https://thangs.com | 1.1M | #45,671 | 14M indexed | Free and paid | Yes |
| 5 | STLFinder | https://www.stlfinder.com | 961K | #42,999 | Not published | Meta-search | **No** |
| 4 | Makeronline | https://www.makeronline.com | 679K | #65,684 | Not published | Free | Yes |
| 3 | Nexprint | https://www.nexprint.com | 383K | #100,313 | Not published | Free | Yes |
| 3 | Pinshape | https://pinshape.com | 187K | #216,438 | 70K+ designers | Free and paid | Yes |
| 2 | YouMagine | https://www.youmagine.com | 77K | #422,347 | ~19K designs | Free only | Yes |

Notes on individual entries:

- **MakerWorld** overtook Thingiverse during 2026 and is now the most visited
  model portal. Growth is driven by Bambu Lab printer sales, creator payouts
  and one-click slicer integration.
- **Printables** has the most consistent license metadata of the large portals,
  which is why its results carry reliable license strings in the plugin.
- **Yeggi** and **STLFinder** are search engines rather than hosts. They index
  the portals above and are the two most significant catalogs the plugin does
  not yet cover.
- **GrabCAD** is a CAD library rather than a print portal. It is kept because
  it is the strongest free source for functional and mechanical parts.
- **Thangs** adds geometric similarity search but has been losing traffic since
  the Shapeways acquisition.
- **YouMagine** is retained for continuity; its traffic is marginal and it now
  shares ownership with Thingiverse and MyMiniFactory.

## Public-domain and institutional catalogs

Low traffic, but every file is openly licensed and print-oriented, so license
resolution is unambiguous.

| Platform | Link | Content | Registered |
|---|---|---|---|
| NIH 3D | https://3d.nih.gov | Anatomy, biomedical, molecular models | Yes |
| Smithsonian 3D | https://3d.si.edu | Museum scans, mostly CC0 | Yes |
| NASA 3D Resources | https://nasa3d.arc.nasa.gov | Spacecraft and planetary models, public domain | Yes |

## Ecosystem changes in 2026

- **MyMiniFactory acquired Thingiverse from UltiMaker on 12 February 2026.**
  Thingiverse, MyMiniFactory and YouMagine now sit under one owner as part of
  the SoulCrafted initiative, which includes a stated plan to reduce and
  eventually remove AI-generated uploads. Existing free models are stated to
  stay free. The plugin keeps three separate adapters for these portals; if the
  APIs converge, that registry entry set is the first thing that will need
  revisiting.
- **MakerWorld passed Thingiverse in monthly traffic**, reversing the ordering
  that held since the plugin was first written.
- **Thangs continues to decline** (-3.8% month over month) following its
  December 2024 acquisition by Shapeways.
- **Nexprint is growing** (+4.8% month over month) on the back of Elegoo's
  creator fund, and is the newest portal in the registry.

## Platforms deliberately excluded

These are large or well known, but are poor fits for a print-oriented search.

| Platform | Visits/mo | Reason for exclusion |
|---|---|---|
| Sketchfab | 14.6M | Viewer and AR asset library; meshes are rarely print-ready |
| CGTrader | 4.6M | Rendering marketplace, mostly paid, few printable topologies |
| Free3D | 2.3M | General CG assets |
| 3DfindIT | 895K | Manufacturer CAD catalogs aimed at engineering documentation |
| 3DExport | 475K | General CG marketplace |
| Wikimedia Commons | n/a | General media archive; 3D files are incidental |
| Zortrax Library | n/a | Small vendor-specific library |
| QIDI Maker | n/a | Launched recently, negligible traffic |

Two of these, **CGTrader** and **Wikimedia Commons**, are still registered in
`src/search_engine.py` for historical reasons. CGTrader is a browser-only
search card and Wikimedia contributes openly licensed STL files, so neither
misrepresents itself as a print catalog, but neither earns its place on merit.

## Coverage gaps

Ordered by the traffic the plugin would gain:

1. **Yeggi** (5.9M) - meta-search over free STL files. Highest-value addition;
   maps cleanly onto the existing adapter shape.
2. **STLFinder** (961K) - second meta-search, same integration pattern.

Everything else worth having is already registered.

---

Survey date: 19 August 2026. Traffic and rankings drift; re-check before
relying on the ordering.
