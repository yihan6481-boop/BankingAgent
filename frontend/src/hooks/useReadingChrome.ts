import {useEffect,useLayoutEffect,useRef,useState} from 'react'
import type {MutableRefObject,RefObject} from 'react'

/** Scroll direction is consumed only after intent inside the message feed.
 * Viewport geometry remains owned exclusively by useMobileViewport. */
export function useReadingChrome({feed,follow,pinned,active,onChange,contentKey,historyLoading}:{feed:RefObject<HTMLDivElement>;follow:MutableRefObject<boolean>;pinned:boolean;active:boolean;onChange?:(hidden:boolean)=>void;contentKey:string;historyLoading?:boolean}){
  const [hidden,setHidden]=useState(false),[keyboard,setKeyboard]=useState(document.documentElement.dataset.keyboardOpen==='true')
  const state=useRef({hidden:false,pinned,active,keyboard});state.current={hidden,pinned,active,keyboard}
  const intentUntil=useRef(0),ignoreUntil=useRef(0),lastTop=useRef(0),travel=useRef(0),lastChange=useRef(0)
  const anchor=useRef<{node:HTMLElement;offset:number;until:number}|null>(null),holdingHistory=useRef(false)
  const capture=()=>{
    const e=feed.current;if(!e)return
    const top=e.getBoundingClientRect().top,node=[...e.querySelectorAll<HTMLElement>('[data-message-id]')].find(n=>n.getBoundingClientRect().bottom>top+4)
    if(node)anchor.current={node,offset:node.getBoundingClientRect().top,until:performance.now()+350}
  }
  const restoreAnchor=()=>{
    const e=feed.current,a=anchor.current;if(!e||!a)return
    if(!a.node.isConnected||performance.now()>a.until&&!holdingHistory.current){anchor.current=null;return}
    const delta=a.node.getBoundingClientRect().top-a.offset
    if(Math.abs(delta)>.5)e.scrollTop+=delta
    lastTop.current=e.scrollTop
  }
  const markProgrammatic=(ms=240)=>{ignoreUntil.current=performance.now()+ms;travel.current=0}
  const change=(value:boolean)=>{
    if(state.current.hidden===value)return
    capture();markProgrammatic();lastChange.current=performance.now();state.current.hidden=value;setHidden(value)
  }
  const show=()=>change(false)
  const latest=()=>{show();follow.current=true;anchor.current=null;holdingHistory.current=false;markProgrammatic();const e=feed.current;if(e)e.scrollTop=e.scrollHeight}
  const beforeHistory=()=>{capture();holdingHistory.current=true;follow.current=false;markProgrammatic(400)}
  useEffect(()=>{
    const root=document.documentElement
    const observer=new window.MutationObserver(()=>setKeyboard(root.dataset.keyboardOpen==='true'))
    observer.observe(root,{attributes:true,attributeFilter:['data-keyboard-open']});return()=>observer.disconnect()
  },[])
  useLayoutEffect(()=>{if(pinned||keyboard||!active)show()},[pinned,keyboard,active])
  useLayoutEffect(()=>{restoreAnchor();if(!historyLoading&&holdingHistory.current){holdingHistory.current=false;if(anchor.current)anchor.current.until=performance.now()+250}},[contentKey,historyLoading,hidden])
  useEffect(()=>{onChange?.(active&&hidden);return()=>onChange?.(false)},[hidden,active,onChange])
  useEffect(()=>{
    const e=feed.current;if(!e)return
    lastTop.current=e.scrollTop
    const intent=()=>{intentUntil.current=performance.now()+900;anchor.current=null;holdingHistory.current=false}
    const keys=(event:KeyboardEvent)=>{if(['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '].includes(event.key))intent()}
    const scroll=()=>{
      const now=performance.now(),delta=e.scrollTop-lastTop.current;lastTop.current=e.scrollTop
      if(now>intentUntil.current||now<ignoreUntil.current||!state.current.active)return
      const bottom=e.scrollHeight-e.clientHeight-e.scrollTop;follow.current=bottom<60
      if(state.current.pinned||state.current.keyboard){show();return}
      if(bottom<48){show();travel.current=0;return}
      if(Math.abs(delta)<1)return
      if(Math.sign(travel.current)!==Math.sign(delta))travel.current=0
      travel.current+=delta
      if(now-lastChange.current<240)return
      if(travel.current<-28&&bottom>100){change(true);travel.current=0}
      else if(travel.current>18){change(false);travel.current=0}
    }
    const resize=()=>{markProgrammatic();restoreAnchor();lastTop.current=e.scrollTop}
    const observer=typeof ResizeObserver!=='undefined'?new ResizeObserver(resize):null;observer?.observe(e)
    e.addEventListener('wheel',intent,{passive:true});e.addEventListener('touchstart',intent,{passive:true});e.addEventListener('touchmove',intent,{passive:true});e.addEventListener('pointerdown',intent,{passive:true});e.addEventListener('keydown',keys);e.addEventListener('scroll',scroll,{passive:true})
    return()=>{observer?.disconnect();e.removeEventListener('wheel',intent);e.removeEventListener('touchstart',intent);e.removeEventListener('touchmove',intent);e.removeEventListener('pointerdown',intent);e.removeEventListener('keydown',keys);e.removeEventListener('scroll',scroll)}
  },[])
  return {hidden,show,latest,beforeHistory,markProgrammatic,restoreAnchor}
}
