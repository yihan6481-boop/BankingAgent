export function originalTurn<T extends {id:string;client_message_id:string}>(turns:T[],target?:{turnId?:string;clientId?:string},pendingClient?:string):T|undefined {
  if(target?.turnId||target?.clientId)return turns.find(t=>(!target.turnId||t.id===target.turnId)&&(!target.clientId||t.client_message_id===target.clientId))
  if(pendingClient)return turns.find(t=>t.client_message_id===pendingClient)
  return turns[turns.length-1]
}
