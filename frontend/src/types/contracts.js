/** @typedef {{id:number, title:string, payload:object, author:string}} BusinessCase */
/** @typedef {{id:number, case_id:number, status:'queued'|'running'|'complete'|'failed'}} Run */
/** @typedef {{id:number, run_id:number, digest:string, report:{summary:string, metrics:object, sections:Array}}} Evidence */
/** @typedef {{id:number, number:string, status:'open'|'acknowledged', terminal:string, item_count:number, items:Array}} PurchaseClaim */
