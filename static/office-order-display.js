/* Display only: recorded USD prices and source-bound market caps; no trade authority. */
(function(root){
  'use strict';
  const number=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0;
  const usd=v=>'$'+v.toLocaleString('en-US',{useGrouping:false,maximumSignificantDigits:8});
  function prices(order){
    const state=order.status??order.state;
    if(['censored','pending'].includes(state)&&order.filled_at==null)return '';
    let exit=order.exit_unit_price_usd;
    if(!number(exit)&&state==='closed'&&number(order.exit_usd)&&Number.isInteger(order.decimals)&&order.decimals>=0&&order.decimals<=18&&typeof order.tokens_atomic==='string'&&/^[0-9]{1,80}$/.test(order.tokens_atomic)&&Number(order.tokens_atomic)>0)exit=order.exit_usd*10**order.decimals/Number(order.tokens_atomic);
    const cap=(value,reason,side)=>number(value)?'mcap '+usd(value):'mcap unavailable: '+(typeof reason==='string'&&reason.trim()?reason.replace(/_/g,' '):'supply at '+side+' not recorded');
    const entry=number(order.entry_unit_price_usd)?usd(order.entry_unit_price_usd)+'/token':'price not recorded';
    return 'Modelled entry: '+entry+' · '+cap(order.entry_mcap_usd,order.entry_mcap_reason,'entry')+' · exit: '+(state==='open'?'not sold':(number(exit)?usd(exit)+'/token':'price unavailable: '+(!number(order.exit_usd)?'exit USD not recorded':'token quantity or decimals not recorded'))+' · '+cap(order.exit_mcap_usd,order.exit_mcap_reason,'exit')+(number(order.exit_mcap_usd)&&order.mcap_basis==='recorded entry supply held constant; modelled'?' (entry supply estimate)':''));
  }
  root.OfficeOrderDisplay={prices};
})(typeof window!=='undefined'?window:globalThis);
