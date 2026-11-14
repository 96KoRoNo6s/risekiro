(function(){
"use strict";
var settingsPromise=null,settingsAt=0,rate=0,rateAt=0,rateKey="";
var BASE="https://portal-market.com/api";
function hex(value){return Array.from(new TextEncoder().encode(value),function(n){return n.toString(16).padStart(2,"0");}).join("");}
function clean(value){return String(value||"").replace(/\s*\([^)]*%\)\s*$/,"").trim();}
function modelImage(collection,model){return model?"https://cdn.tgmrkt.io/gifts/stickers/thumbnails/"+hex(collection+"_"+model)+".webp":"";}
function modelAnimation(collection,model){return model?"https://cdn.tgmrkt.io/gifts/stickers/"+hex(collection+"_"+model)+".json":"";}
function symbolImage(collection,symbol){return symbol?"https://api.changes.tg/symbol/"+encodeURIComponent(collection)+"/"+encodeURIComponent(symbol)+".png?size=128":"";}
async function json(url){var response=await fetch(url,{cache:"no-store"});if(!response.ok)throw new Error("Catalog request failed");return response.json();}
async function settings(){
  if(!settingsPromise||Date.now()-settingsAt>60000){settingsAt=Date.now();settingsPromise=json("catalog-config.json").catch(function(e){settingsPromise=null;throw e;});}
  return settingsPromise;
}
async function rubRate(){
  var cfg=await settings();
  var key=JSON.stringify([cfg.usd_rub_rate,cfg.exchange_fee_percent,cfg.pricing_updated_at]),exchangeFee=Number(cfg.exchange_fee_percent)/100;
  if(!(exchangeFee>=0&&exchangeFee<1))throw new Error("Invalid exchange fee");
  if(rate>0&&Date.now()-rateAt<300000&&rateKey===key)return rate;
  try{var data=await json("https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd");var value=Number(data["the-open-network"].usd)*Number(cfg.usd_rub_rate)/(1-exchangeFee);if(value>0&&Number.isFinite(value)){rate=value;rateKey=key;rateAt=Date.now();return rate;}}catch(_){}
  if(Number(cfg.ton_rub_rate_snapshot)>0){rate=Number(cfg.ton_rub_rate_snapshot);rateKey=key;rateAt=Date.now();return rate;}
  throw new Error("Currency rate unavailable");
}
function price(ton,exchange,cfg){
  var starsTon=Number(cfg.fragment_stars_package_ton)*Number(cfg.transfer_stars)/Number(cfg.fragment_stars_package_stars);
  if(!(starsTon>0&&Number.isFinite(starsTon)))throw new Error("Invalid transfer price");
  var cost=(Number(ton)+starsTon)*exchange;
  var withdrawal=Number(cfg.withdrawal_fee_percent)/100;
  if(!(withdrawal>=0&&withdrawal<1))throw new Error("Invalid pricing settings");
  var net=Math.ceil((cost*(1+Number(cfg.margin_percent)/100)/(1-withdrawal))*100-1e-9)/100;
  return Math.ceil((net*Number(cfg.checkout_multiplier))/5-1e-9)*5;
}
async function collections(){
  var data=await json(BASE+"/collections?limit=500"),cfg=await settings(),exchange=await rubRate();
  return (data.collections||[]).filter(function(c){return !cfg.balance_limit_enabled||(Number(c.floor_price)>0&&Number(c.floor_price)<=Number(cfg.max_purchase_price_ton));}).map(function(c){return {id:c.id,name:c.name,short_name:c.short_name,image:c.photo_url||"https://nft.fragment.com/gift/"+String(c.short_name||c.name).replace(/[^a-z0-9]/gi,"").toLowerCase()+"-1.webp",floor_price_rub:Number(c.floor_price)>0?price(c.floor_price,exchange,cfg):null};}).sort(function(a,b){return a.name.localeCompare(b.name);});
}
async function attributes(collection){
  if(!collection)return {models:[],symbols:[]};
  var rows=await Promise.allSettled([json("https://api.changes.tg/models/"+encodeURIComponent(collection)),json("https://api.changes.tg/symbols/"+encodeURIComponent(collection))]);
  var models=rows[0].status==="fulfilled"&&Array.isArray(rows[0].value)?rows[0].value:[];
  var symbols=rows[1].status==="fulfilled"&&Array.isArray(rows[1].value)?rows[1].value:[];
  return {models:models.map(function(m){var name=clean(m.name);return{name:name,image:modelImage(collection,name)};}),symbols:symbols.map(function(s){var name=clean(s.name);return{name:name,image:symbolImage(collection,name)};})};
}
async function search(options,collectionRows,backdrops){
  var p=new URLSearchParams({limit:"50",offset:String(options.offset||0),sort_by:options.sort==="desc"?"price desc":"price asc",status:"listed",exclude_bundled:"true",premarket_status:"without_premarket"});
  var col=options.collection||"",number=String(options.number||"").trim();
  var match=number.match(/^([A-Za-z][A-Za-z0-9]*)-(\d+)$/);
  if(match){var found=collectionRows.find(function(c){return c.name.replace(/[^A-Za-z0-9]/g,"").toLowerCase()===match[1].toLowerCase();});if(found)col=found.name;number=match[2];}
  var row=collectionRows.find(function(c){return c.name===col;});if(row)p.set("collection_ids",row.id);
  if(options.model)p.set("filter_by_models",options.model);
  if(options.backdrop)p.set("filter_by_backdrops",options.backdrop);
  if(options.symbol)p.set("filter_by_symbols",options.symbol);
  if(/^#?\d+$/.test(number))p.set("external_collection_number",number.replace("#",""));
  var cfg=await settings(),exchange=await rubRate();
  if(cfg.balance_limit_enabled)p.set("max_price",String(cfg.max_purchase_price_ton));
  var data=await json(BASE+"/nfts/search?"+p);
  if(cfg.balance_limit_enabled&&Date.now()/1000-Number(cfg.budget_updated_at||0)>1200)throw new Error("Обновляем предложения. Попробуйте немного позже.");
  var raw=Array.isArray(data.results)?data.results:[];
  var items=raw.filter(function(r){return r.status==="listed"&&!r.bundle_id&&Number(r.price)>0&&(!cfg.balance_limit_enabled||Number(r.price)<=Number(cfg.max_purchase_price_ton));}).map(function(r){
    var attrs={},rarities={};(r.attributes||[]).forEach(function(a){attrs[a.type]=clean(a.value);if(a.rarity_per_mille!=null)rarities[a.type]=String(Number(a.rarity_per_mille)/10);});
    var collection=r.name||col,model=attrs.model||"",symbol=attrs.symbol||"",backdrop=attrs.backdrop||"";
    var number=r.external_collection_number||String(r.tg_id||"").split("-").pop();
    var code=/^[A-Za-z0-9]+-\d+$/.test(r.tg_id||"")?r.tg_id:collection.replace(/[^A-Za-z0-9]/g,"")+"-"+number;
    var colors=backdrops[backdrop.toLowerCase()]||["",""];
    return {name:code,number:String(number||""),collection:collection,model:model,backdrop:backdrop,symbol:symbol,
      model_rarity:rarities.model||"",backdrop_rarity:rarities.backdrop||"",symbol_rarity:rarities.symbol||"",
      price_rub:price(r.price,exchange,cfg),model_image:modelImage(collection,model),animation_url:"https://nft.fragment.com/gift/"+code.toLowerCase()+".lottie.json",
      symbol_image:symbolImage(collection,symbol),backdrop_center:colors[0],backdrop_edge:colors[1],gift_url:"https://t.me/nft/"+code};
  }).filter(function(i){return(!options.model||i.model===options.model)&&(!options.backdrop||i.backdrop===options.backdrop)&&(!options.symbol||i.symbol===options.symbol)&&(!options.min||i.price_rub>=Number(options.min))&&(!options.max||i.price_rub<=Number(options.max));});
  return {items:items,next:[raw.length===50?Number(options.offset||0)+50:null,null,null],resolved_collection:col};
}
function refresh(){settingsPromise=null;settingsAt=0;rate=0;rateAt=0;rateKey="";return settings();}
window.CatalogPublic={collections:collections,attributes:attributes,search:search,refresh:refresh};
})();
