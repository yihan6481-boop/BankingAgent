export interface PhoneBinding {session_id:string;mode:'phone';phone:string;request_sequence:number;resolution_id:string;revision:number}
export interface PhoneContext {sid:string;mode:string;phone:string;account:string;name:string;card:string}
export interface PhoneTicket extends PhoneContext {sequence:number}
export function phoneResolutionGuard(initial:PhoneContext){
 let context=initial,sequence=0,live=true,bound:PhoneBinding|null=null
 const same=(a:PhoneContext,b:PhoneContext)=>a.sid===b.sid&&a.mode===b.mode&&a.phone===b.phone&&a.account===b.account&&a.name===b.name&&a.card===b.card
 const invalidate=()=>{sequence++;bound=null}
 const accepts=(ticket:PhoneTicket)=>live&&ticket.sequence===sequence&&same(ticket,context)&&context.mode==='phone'
 return {
  update(value:PhoneContext){if(!same(value,context)){context=value;invalidate()}},
  invalidate,activate(){live=true},dispose(){live=false;invalidate()},
  begin(){invalidate();return {...context,sequence}},accepts,
  bind(ticket:PhoneTicket,value:{resolution_id:string;revision:number;request_sequence:number}){
   if(!accepts(ticket)||value.request_sequence!==ticket.sequence||!/^rr_[a-f0-9]{32}$/.test(value.resolution_id)||!Number.isInteger(value.revision)||value.revision<1)return false
   bound={session_id:ticket.sid,mode:'phone',phone:ticket.phone,request_sequence:ticket.sequence,resolution_id:value.resolution_id,revision:value.revision};return true
  },
  binding(){return live&&context.mode==='phone'&&bound?.phone===context.phone&&bound.session_id===context.sid&&bound.request_sequence===sequence?bound:null},
 }
}
