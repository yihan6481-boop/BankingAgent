import { useEffect,useRef } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
export function BottomSheet({title,onClose,children}:{title:string;onClose:()=>void;children:ReactNode}){
 const root=useRef<HTMLElement>(null),close=useRef(onClose);close.current=onClose
 useEffect(()=>{
  const previous=document.activeElement as HTMLElement|null
  const nodes=()=>Array.from(root.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),a[href]')||[])
  nodes()[0]?.focus({preventScroll:true})
  const key=(e:KeyboardEvent)=>{if(e.key==='Escape'){e.preventDefault();close.current()}if(e.key==='Tab'){const list=nodes(),first=list[0],last=list[list.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus()}}}
  document.addEventListener('keydown',key);return()=>{document.removeEventListener('keydown',key);if(previous?.isConnected)previous.focus({preventScroll:true})}
 },[])
 return createPortal(<div className="next-sheet-shade" onClick={onClose}><section ref={root} className="next-sheet" role="dialog" aria-modal="true" aria-label={title} onClick={e=>e.stopPropagation()}><div className="sheet-grip"/><button className="sheet-close" aria-label="关闭提醒" onClick={onClose}>×</button>{children}</section></div>,document.body)
}
