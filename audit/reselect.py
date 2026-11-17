"""Re-run v0.2.1 selection on the cached Trailer 2 analysis (no re-transcribe)."""
import json, sys
sys.path.insert(0, '/home/hatch/workspace/gta6-clipkit')
import clipkit

cache = json.load(open('/home/hatch/workspace/gta6-clipkit/out-trailer2/analysis.json'))
cues, cuts = cache['cues'], cache['cuts']
src = '/home/hatch/workspace/gta6-clipping/trailer2.mp4'
cands = clipkit.build_candidates(cues, cuts, cache['duration'])
print(f'candidates: {len(cands)}')
scored = []
for cand in cands:
    score, parts, text = clipkit.score_window(src, cand, cues, cuts)
    scored.append({**cand, 'score': score, 'parts': parts, 'text': text})
    print(f"  {cand['start']:6.1f}-{cand['end']:6.1f} ({cand['end']-cand['start']:4.1f}s) "
          f"score {score:.3f} density {parts['speech_density_wps']:.2f} wps "
          f"max_gap {parts['max_gap_s']:.1f}s words {parts['words']} "
          f"hook '{clipkit.hook_line(text)}'")
scored.sort(key=lambda x: x['score'], reverse=True)
print('--- new ranking ---')
for i, c in enumerate(scored, 1):
    print(f"#{i} {c['start']:.1f}-{c['end']:.1f} score {c['score']:.3f} max_gap {c['parts']['max_gap_s']:.1f}s")
json.dump({'candidates': scored}, open('/home/hatch/workspace/gta6-clipkit/audit/reselect.json', 'w'), indent=2)
# update the cached analysis so CLI edit mode renders the fixed ranking
cache['candidates'] = scored
json.dump(cache, open('/home/hatch/workspace/gta6-clipkit/out-trailer2/analysis.json', 'w'))
print('analysis.json candidates updated')
