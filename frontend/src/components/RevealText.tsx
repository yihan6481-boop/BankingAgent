import {useLayoutEffect,useRef} from 'react'
import type {CSSProperties} from 'react'

/** Animate appended display text only; this component never schedules typing. */
export function RevealText({text,animate=false}:{text:string;animate?:boolean}){
  const previous=useRef(animate?'':text),unchanged=text.startsWith(previous.current)?previous.current.length:0
  useLayoutEffect(()=>{previous.current=text},[text])
  if(!animate||unchanged===text.length)return <>{text}</>
  const fresh=text.slice(unchanged),Segmenter=(Intl as any).Segmenter
  const graphemes=(value:string):string[]=>Segmenter?Array.from(new Segmenter('zh',{granularity:'grapheme'}).segment(value),(v:any)=>v.segment):[value]
  // Numeric tokens stay together. Grapheme segmentation preserves emoji/ZWJ.
  const tokens=fresh.split(/(\d+(?:[.,]\d+)*%?)/u).flatMap(v=>/^\d/.test(v)?[v]:graphemes(v))
  const stride=Math.max(1,Math.ceil(tokens.length/48)),chunks:string[]=[]
  for(let i=0;i<tokens.length;i+=stride)chunks.push(tokens.slice(i,i+stride).join(''))
  const light=navigator.hardwareConcurrency>0&&navigator.hardwareConcurrency<=4
  return <>{text.slice(0,unchanged)}{chunks.map((chunk,i)=><span className={`reply-reveal${light?' reply-reveal-light':''}`} style={{'--reveal-delay':`${Math.min(i*20,120)}ms`} as CSSProperties} key={unchanged+':'+i}>{chunk}</span>)}</>
}
