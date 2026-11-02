import type { ReactNode } from 'react'
import {RevealText} from './RevealText'

// A small presentation-only Markdown subset. React escapes every text node;
// no HTML parser, dangerous HTML, external images or executable URLs.
function inline(text:string,animate=false):ReactNode[]{
  return text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g).map((s,i)=>{
    if(s.startsWith('**')&&s.endsWith('**'))return <strong key={i}><RevealText text={s.slice(2,-2)} animate={animate}/></strong>
    if(s.startsWith('`')&&s.endsWith('`'))return <code key={i}><RevealText text={s.slice(1,-1)} animate={animate}/></code>
    const link=/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/.exec(s)
    return link?<a key={i} href={link[2]} target="_blank" rel="noopener noreferrer"><RevealText text={link[1]} animate={animate}/></a>:<RevealText key={i} text={s} animate={animate}/>
  })
}
export function SafeMarkdown({text,animate=false}:{text:string;animate?:boolean}){
  return <div className="safe-markdown">{text.split('\n').map((line,i)=>{
    const heading=/^#{1,4}\s+(.+)$/.exec(line)
    if(heading)return <p className="message-heading" key={i}>{inline(heading[1],animate)}</p>
    const bullet=/^\s*(?:[-*]|\d+\.)\s+(.+)$/.exec(line)
    return bullet?<p className="message-bullet" key={i}><span aria-hidden="true">•</span>{inline(bullet[1],animate)}</p>:<p key={i}>{inline(line,animate)||'\u00a0'}</p>
  })}</div>
}
