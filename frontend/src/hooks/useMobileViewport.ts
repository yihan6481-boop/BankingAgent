import {useEffect} from 'react'

export interface ViewportSample {height:number;width:number;layoutHeight:number;layoutWidth:number;top:number;scale:number;editable:boolean}
export interface ViewportState {baseline:number;width:number;keyboard:boolean;height:number;top:number}
/** One measured visual rectangle, never a guessed keyboard padding. */
export function viewportState(previous:ViewportState|null,s:ViewportSample):ViewportState {
  const rotated=!!previous&&Math.abs(s.layoutWidth-previous.width)>40
  const normal=Math.abs(s.scale-1)<.02
  const baseline=!previous||rotated?Math.max(s.layoutHeight,s.height):Math.max(previous.baseline,s.layoutHeight,normal?s.height:0)
  const deficit=baseline-s.height
  const keyboard=normal&&!rotated&&deficit>Math.max(140,baseline*.22)&&
    s.width>=s.layoutWidth*.9&&(s.editable||!!previous?.keyboard)
  return {baseline,width:s.layoutWidth,keyboard,height:normal?s.height:previous?.height||s.layoutHeight,
    top:normal?Math.max(0,s.top):previous?.top||0}
}

export function useMobileViewport(){
  useEffect(()=>{
    let state:ViewportState|null=null,frame=0
    const root=document.documentElement,v=window.visualViewport
    const raf=window.requestAnimationFrame?.bind(window)||((fn:FrameRequestCallback)=>window.setTimeout(()=>fn(0),0))
    const cancel=window.cancelAnimationFrame?.bind(window)||window.clearTimeout.bind(window)
    const oldHeight=root.style.getPropertyValue('--usable-height'),oldTop=root.style.getPropertyValue('--viewport-top')
    const oldKeyboard=root.dataset.keyboardOpen
    const oldOwner=root.dataset.mobileViewport
    root.dataset.mobileViewport='active'
    const update=()=>{
      const e=document.activeElement as HTMLElement|null
      const editable=!!e&&(e.matches('input:not([type=checkbox]):not([type=radio]):not([type=button]),textarea')||e.isContentEditable)
      state=viewportState(state,{height:v?.height||window.innerHeight,width:v?.width||window.innerWidth,
        layoutHeight:document.documentElement.clientHeight||window.innerHeight,layoutWidth:document.documentElement.clientWidth||window.innerWidth,
        top:v?.offsetTop||0,scale:v?.scale||1,editable})
      root.style.setProperty('--usable-height',`${state.height}px`)
      root.style.setProperty('--viewport-top',`${state.top}px`)
      root.dataset.keyboardOpen=String(state.keyboard)
      // Move only a clipped form field inside its own scroll container. Never
      // scroll the document or a reader's chat history on each keystroke.
      const form=e?.closest<HTMLElement>('.mobile-auth')
      if(state.keyboard&&form&&e){const box=e.getBoundingClientRect(),area=form.getBoundingClientRect()
        if(box.bottom>area.bottom-12)form.scrollTop+=box.bottom-area.bottom+12
        else if(box.top<area.top+12)form.scrollTop+=box.top-area.top-12
      }
    }
    const schedule=()=>{cancel(frame);frame=raf(update)}
    update()
    v?.addEventListener('resize',schedule);v?.addEventListener('scroll',schedule)
    window.addEventListener('resize',schedule);window.addEventListener('orientationchange',schedule)
    document.addEventListener('focusin',schedule);document.addEventListener('focusout',schedule)
    return()=>{cancel(frame);v?.removeEventListener('resize',schedule);v?.removeEventListener('scroll',schedule)
      window.removeEventListener('resize',schedule);window.removeEventListener('orientationchange',schedule)
      document.removeEventListener('focusin',schedule);document.removeEventListener('focusout',schedule)
      if(oldHeight)root.style.setProperty('--usable-height',oldHeight);else root.style.removeProperty('--usable-height')
      if(oldTop)root.style.setProperty('--viewport-top',oldTop);else root.style.removeProperty('--viewport-top')
      if(oldKeyboard===undefined)delete root.dataset.keyboardOpen;else root.dataset.keyboardOpen=oldKeyboard
      if(oldOwner===undefined)delete root.dataset.mobileViewport;else root.dataset.mobileViewport=oldOwner
    }
  },[])
}
