# State Portal Patterns — All 50 States

Search patterns for locating NPDES permit documents by state. The NPDES permit number prefix (first two letters) identifies the state. Always start with EPA ECHO for document links; fall back to the state portal when ECHO returns none.

---

## EPA ECHO API — Universal Starting Point

Always query ECHO first, regardless of state. ECHO has facility metadata, permit dates, compliance history, and direct document links for all NPDES-permitted facilities.

### Working endpoints (confirmed as of 2026)

```
# Detailed Facility Report — returns identity, permits, compliance, document links
https://echodata.epa.gov/echo/dfr_rest_services.get_dfr?p_id={NPDES}&output=JSON

# CWA effluent compliance history
https://echodata.epa.gov/echo/dfr_rest_services.get_cwa_eff_compliance?p_id={NPDES}&output=JSON
```

Check `Results.WebFireDocuments` and `Results.CAEDDocuments` in the DFR response for direct PDF links. When a direct link is found, use it before trying any state portal.

---

## NPDES Prefix → State Mapping

| Prefix | State | Agency |
|--------|-------|--------|
| AL | Alabama | ADEM |
| AK | Alaska | ADEC |
| AZ | Arizona | ADEQ |
| AR | Arkansas | ADEQ (AR) |
| CA | California | SWRCB |
| CO | Colorado | CDPHE |
| CT | Connecticut | DEEP |
| DE | Delaware | DNREC |
| FL | Florida | FDEP |
| GA | Georgia | EPD |
| HI | Hawaii | DOH |
| ID | Idaho | DEQ (ID) |
| IL | Illinois | IEPA |
| IN | Indiana | IDEM |
| IA | Iowa | DNR (IA) |
| KS | Kansas | KDHE |
| KY | Kentucky | DEP (KY) |
| LA | Louisiana | LDEQ |
| ME | Maine | DEP (ME) |
| MD | Maryland | MDE |
| MA | Massachusetts | MassDEP |
| MI | Michigan | EGLE |
| MN | Minnesota | MPCA |
| MS | Mississippi | MDEQ |
| MO | Missouri | MoDNR |
| MT | Montana | DEQ (MT) |
| NE | Nebraska | NDEE |
| NV | Nevada | NDEP |
| NH | New Hampshire | DES (NH) |
| NJ | New Jersey | NJDEP |
| NM | New Mexico | NMED |
| NY | New York | NYSDEC |
| NC | North Carolina | NCDEQ |
| ND | North Dakota | NDDEQ |
| OH | Ohio | Ohio EPA |
| OK | Oklahoma | ODEQ |
| OR | Oregon | DEQ (OR) |
| PA | Pennsylvania | PaDEP |
| RI | Rhode Island | DEM (RI) |
| SC | South Carolina | SCDHEC |
| SD | South Dakota | DANR |
| TN | Tennessee | TDEC |
| TX | Texas | TCEQ |
| UT | Utah | DWQ |
| VT | Vermont | DEC (VT) |
| VA | Virginia | DEQ (VA) |
| WA | Washington | Ecology |
| WV | West Virginia | WVDEP |
| WI | Wisconsin | WDNR |
| WY | Wyoming | WDEQ |

---

## State-by-State Portal Reference

---

### AL — Alabama — ADEM

**Agency:** Alabama Department of Environmental Management  
**Portal:** https://epa.adem.alabama.gov/epa/  
**Permit search:** https://epa.adem.alabama.gov/epa/permitSearch.do  
**Direct PDF pattern:** None predictable. Portal navigation required.  
**Download behavior:** Portal only — direct PDF URLs time out or return 403.

How to find: Enter permit number → click result → download Final Permit, Fact Sheet, Application.

---

### AK — Alaska — ADEC

**Agency:** Alaska Department of Environmental Conservation  
**Portal:** https://dec.alaska.gov/water/wastewater/permits/  
**Permit search:** https://dec.alaska.gov/water/wastewater/permits/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required. Alaska issues APDES permits (Alaska Pollutant Discharge Elimination System).  
**Note:** APDES permit numbers begin with AK.

---

### AZ — Arizona — ADEQ

**Agency:** Arizona Department of Environmental Quality  
**Portal:** https://azdeq.gov/node/193  
**Permit search:** https://gisweb.azdeq.gov/azdocdbpub/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required. AZPDES permits.

---

### AR — Arkansas — ADEQ (AR)

**Agency:** Arkansas Division of Environmental Quality  
**Portal:** https://www.adeq.state.ar.us/water/permits/  
**Permit search:** https://www.adeq.state.ar.us/water/permits/arapdes/default.aspx  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required. ARAPDES permits.

---

### CA — California — SWRCB

**Agency:** California State Water Resources Control Board  
**Portal:** https://www.waterboards.ca.gov/water_issues/programs/npdes/  
**Permit search (SMARTS):** https://smarts.waterboards.ca.gov/smarts/faces/SwSmartsLogin.xhtml  
**eSMARTS documents:** https://www.waterboards.ca.gov/water_issues/programs/npdes/  
**Direct PDF pattern:** None predictable from permit number alone.  
**Download behavior:** Portal navigation required. Many permits are issued by Regional Water Quality Control Boards (9 regions), not the state board directly. Check the regional board website for the relevant region.  
**Regional boards:** https://www.waterboards.ca.gov/about_us/

---

### CO — Colorado — CDPHE

**Agency:** Colorado Department of Public Health and Environment  
**Portal:** https://cdphe.colorado.gov/water-quality/permits  
**Permit search:** https://www.colorado.gov/pacific/cdphe/wqcd-wastewater-permit-look-up  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required. CDPS (Colorado Discharge Permit System) permits.

---

### CT — Connecticut — DEEP

**Agency:** Connecticut Department of Energy and Environmental Protection  
**Portal:** https://portal.ct.gov/DEEP/Water/Permits-Registrations-Certifications/NPDES-Permits  
**Permit search:** https://www.ct.gov/deep/cwp/view.asp?a=2709&q=324218  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### DE — Delaware — DNREC

**Agency:** Delaware Department of Natural Resources and Environmental Control  
**Portal:** https://dnrec.delaware.gov/water/permits/  
**Permit search:** https://dnrec.delaware.gov/water/permits/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### FL — Florida — FDEP

**Agency:** Florida Department of Environmental Protection  
**Portal:** https://www.floridadep.gov/water/domestic-wastewater  
**OCULUS document search:** https://oculus.dep.state.fl.us/  
**Direct PDF pattern:** None predictable from permit number alone.  
**Download behavior:** OCULUS requires search by facility name or permit number. Florida permits are organized by permit type (Domestic Wastewater, Industrial Wastewater). No direct PDF URL construction from permit number.

How to find: Go to OCULUS → search by permit number or facility name → select document type → download.

---

### GA — Georgia — EPD

**Agency:** Georgia Environmental Protection Division  
**Portal:** https://epd.georgia.gov/watershed-protection-branch/npdes-permits  
**EPD Online:** https://gaepd.force.com/EPDonline/s/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation via EPD Online required.

How to find: EPD Online → search by permit number → download Final Permit and Fact Sheet.

---

### HI — Hawaii — DOH

**Agency:** Hawaii Department of Health, Clean Water Branch  
**Portal:** https://health.hawaii.gov/cwb/  
**Permit search:** https://health.hawaii.gov/cwb/site-navigation/national-pollutant-discharge-elimination-system/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### ID — Idaho — DEQ (ID)

**Agency:** Idaho Department of Environmental Quality  
**Portal:** https://www.deq.idaho.gov/water-quality/wastewater/wastewater-land-application-permits/  
**Permit search:** https://www.deq.idaho.gov/water-quality/wastewater/wastewater-land-application-permits/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### IL — Illinois — IEPA

**Agency:** Illinois Environmental Protection Agency  
**Portal:** https://www2.illinois.gov/epa/topics/water-quality/permits/Pages/default.aspx  
**iGO permit search:** https://www2.illinois.gov/epa/topics/water-quality/permits/Pages/default.aspx  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### IN — Indiana — IDEM

**Agency:** Indiana Department of Environmental Management  
**Portal:** https://www.in.gov/idem/cleanwater/  
**Virtual File Cabinet:** https://vfc.idem.in.gov/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** IDEM Virtual File Cabinet is searchable by permit number. Permits often available as PDFs but URLs are session-based.

---

### IA — Iowa — DNR (IA)

**Agency:** Iowa Department of Natural Resources  
**Portal:** https://www.iowadnr.gov/Environmental-Protection/Water-Quality/NPDES-Permits  
**Permit search:** https://programs.iowadnr.gov/npdespermits/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### KS — Kansas — KDHE

**Agency:** Kansas Department of Health and Environment  
**Portal:** https://www.kdhe.ks.gov/1340/NPDES-Permits  
**Permit search:** https://www.kdhe.ks.gov/1340/NPDES-Permits  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### KY — Kentucky — DEP (KY)

**Agency:** Kentucky Energy and Environment Cabinet, Department for Environmental Protection  
**Portal:** https://eec.ky.gov/Environmental-Protection/Water/Permits/Pages/NPDES.aspx  
**Permit search:** https://eec.ky.gov/Environmental-Protection/Water/Permits/Pages/KPDES-Permit-Search.aspx  
**Direct PDF pattern:** None predictable.  
**Download behavior:** KPDES (Kentucky Pollutant Discharge Elimination System). Portal navigation required.

---

### LA — Louisiana — LDEQ

**Agency:** Louisiana Department of Environmental Quality  
**Portal:** https://www.ldeq.louisiana.gov/page/surface-water-permits  
**EDMS search:** https://edms.deq.louisiana.gov/  
**Direct PDF pattern:** None predictable from permit number alone.  
**Download behavior:** EDMS (Electronic Document Management System) is searchable. Search by facility name or AI number. Water permits are under the Water Permits category.

---

### ME — Maine — DEP (ME)

**Agency:** Maine Department of Environmental Protection  
**Portal:** https://www.maine.gov/dep/water/licensing/wdp/  
**Permit search:** https://www.maine.gov/dep/water/licensing/wdp/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### MD — Maryland — MDE

**Agency:** Maryland Department of the Environment  
**Portal:** https://mde.maryland.gov/programs/water/Pages/waterManagement.aspx  
**Permit search:** https://mde.maryland.gov/programs/water/Pages/npdesPermitsSearch.aspx  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### MA — Massachusetts — MassDEP

**Agency:** Massachusetts Department of Environmental Protection  
**Portal:** https://www.mass.gov/npdes-permits  
**eDEP system:** https://edep.dep.mass.gov/Pages/Welcome.aspx  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### MI — Michigan — EGLE

**Agency:** Michigan Department of Environment, Great Lakes, and Energy  
**Portal:** https://www.michigan.gov/egle/regulatory-assistance/permits/surface-water-permits  
**Permit search:** https://www.michigan.gov/egle/regulatory-assistance/permits/surface-water-permits/wpdes-permit-search  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### MN — Minnesota — MPCA

**Agency:** Minnesota Pollution Control Agency  
**Portal:** https://www.pca.state.mn.us/business-with-us/permits  
**Permit search:** https://www.pca.state.mn.us/business-with-us/permit-search  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### MS — Mississippi — MDEQ

**Agency:** Mississippi Department of Environmental Quality  
**Portal:** https://www.mdeq.ms.gov/water/surface-water/wastewater/npdes/  
**Permit search:** https://www.mdeq.ms.gov/water/surface-water/wastewater/npdes/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** MDEQ often posts permits as PDFs accessible through the permit search interface. Portal navigation required.

---

### MO — Missouri — MoDNR

**Agency:** Missouri Department of Natural Resources  
**Portal:** https://dnr.mo.gov/water/business-industry-other-entities/permits-certification-engineering-fees-forms/state-operating-permits  
**Permit search:** https://dnr.mo.gov/water/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### MT — Montana — DEQ (MT)

**Agency:** Montana Department of Environmental Quality  
**Portal:** https://deq.mt.gov/water/permits  
**Permit search:** https://deq.mt.gov/water/permits  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### NE — Nebraska — NDEE

**Agency:** Nebraska Department of Environment and Energy  
**Portal:** https://dee.ne.gov/NDEQProg.nsf/onweb/NPDES  
**Permit search:** https://dee.ne.gov/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### NV — Nevada — NDEP

**Agency:** Nevada Division of Environmental Protection  
**Portal:** https://ndep.nv.gov/water/permits/water-pollution-permits  
**Permit search:** https://ndep.nv.gov/water/permits/water-pollution-permits  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### NH — New Hampshire — DES (NH)

**Agency:** New Hampshire Department of Environmental Services  
**Portal:** https://www.des.nh.gov/water/wastewater/  
**Permit search:** https://www.des.nh.gov/water/wastewater/permits.htm  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### NJ — New Jersey — NJDEP

**Agency:** New Jersey Department of Environmental Protection  
**Portal:** https://www.nj.gov/dep/dwq/njpdes_guide.htm  
**Permit search:** https://www13.state.nj.us/DataMiner/Search/SearchByCategory?isExternal=Y&getCategory=NJPDES%20Permits  
**Direct PDF pattern:** None predictable.  
**Download behavior:** NJPDES (New Jersey Pollutant Discharge Elimination System). Portal navigation required.

---

### NM — New Mexico — NMED

**Agency:** New Mexico Environment Department  
**Portal:** https://www.env.nm.gov/water-quality/surface-water-quality-bureau/npdes/  
**Permit search:** https://www.env.nm.gov/water-quality/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### NY — New York — NYSDEC

**Agency:** New York State Department of Environmental Conservation  
**Portal:** https://www.dec.ny.gov/chemical/9233.html  
**eDEC system:** https://extapps.dec.ny.gov/cfmx/extapps/envapps/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** SPDES (State Pollutant Discharge Elimination System). Permits are issued by DEC. Portal navigation via eDEC required. Many large permits are posted on DEC's website as searchable PDFs.

---

### NC — North Carolina — NCDEQ

**Agency:** North Carolina Department of Environmental Quality, Division of Water Resources  
**Portal:** https://www.deq.nc.gov/about/divisions/water-resources/water-resources-permits/wastewater-branch  
**EDOCS system:** https://edocs.deq.nc.gov/WaterResources/  
**Direct PDF pattern:** Some permits accessible via EDOCS; search by permit number in the EDOCS system. No predictable URL construction from permit number alone.  
**Download behavior:** EDOCS is searchable and allows PDF downloads. Better than most states for direct retrieval once you have the document ID.

---

### ND — North Dakota — NDDEQ

**Agency:** North Dakota Department of Environmental Quality  
**Portal:** https://www.deq.nd.gov/wq/permits/  
**Permit search:** https://www.deq.nd.gov/wq/permits/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### OH — Ohio — Ohio EPA

**Agency:** Ohio Environmental Protection Agency, Division of Surface Water  
**Portal:** https://epa.ohio.gov/divisions-and-offices/division-of-surface-water/permit-programs/npdes  
**ePortal:** https://ohioepa.custhelp.com/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Ohio EPA has an online permit lookup but PDF downloads require portal navigation.

---

### OK — Oklahoma — ODEQ

**Agency:** Oklahoma Department of Environmental Quality  
**Portal:** https://www.deq.ok.gov/divisions/wqd/permits-and-permits-engineering/  
**Permit search:** https://www.deq.ok.gov/divisions/wqd/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### OR — Oregon — DEQ (OR)

**Agency:** Oregon Department of Environmental Quality  
**Portal:** https://www.oregon.gov/deq/water/Pages/WaterPermits.aspx  
**ePermitting:** https://permits.oregon.gov/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Oregon ePermitting system is searchable. Portal navigation required.

---

### PA — Pennsylvania — PaDEP

**Agency:** Pennsylvania Department of Environmental Protection  
**Portal:** https://www.dep.pa.gov/Business/Water/CleanWater/WasteWater/Pages/NPDES.aspx  
**eFACTS system:** https://www.dep.pa.gov/DataandTools/Pages/eFACTS.aspx  
**Direct PDF pattern:** None predictable from permit number alone.  
**Download behavior:** eFACTS (Environmental Facility Application and Compliance Tracking System) is searchable. Permits often available as PDFs through eFACTS, but document IDs are needed, not permit numbers directly.

---

### RI — Rhode Island — DEM (RI)

**Agency:** Rhode Island Department of Environmental Management  
**Portal:** https://dem.ri.gov/programs/water/permits/  
**Permit search:** https://dem.ri.gov/programs/water/permits/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### SC — South Carolina — SCDHEC

**Agency:** South Carolina Department of Health and Environmental Control  
**Portal:** https://www.scdhec.gov/environment/water-pollution/scpdes-permits  
**Permit search:** https://www.scdhec.gov/environment/water-pollution/scpdes-permits  
**Direct PDF pattern:** None predictable.  
**Download behavior:** SCPDES (South Carolina Pollutant Discharge Elimination System). Portal navigation required.

---

### SD — South Dakota — DANR

**Agency:** South Dakota Department of Agriculture and Natural Resources  
**Portal:** https://danr.sd.gov/water/watersheds/  
**Permit search:** https://danr.sd.gov/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### TN — Tennessee — TDEC

**Agency:** Tennessee Department of Environment and Conservation  
**Portal:** https://www.tn.gov/environment/program-areas/wr-water-resources/water-quality/npdes-permits.html  
**Virtual file cabinet:** https://tdec.tn.gov/epd/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Virtual file cabinet is searchable by permit number. Portal navigation required.

---

### TX — Texas — TCEQ

**Agency:** Texas Commission on Environmental Quality  
**Portal:** https://www.tceq.texas.gov/permitting/water_supply/wq_authorization/  
**STEERS system:** https://www15.tceq.texas.gov/crpub/index.cfm?fuseaction=iwr.main  
**Direct PDF pattern:** None predictable from permit number alone.  
**Download behavior:** STEERS contains permit applications and issued permits. TCEQ Central Registry also has documents. Portal navigation required.

---

### UT — Utah — DWQ

**Agency:** Utah Division of Water Quality, Department of Environmental Quality  
**Portal:** https://deq.utah.gov/water-quality/utah-pollutant-discharge-elimination-system-updes-permits  
**Permit search:** https://deq.utah.gov/water-quality/utah-pollutant-discharge-elimination-system-updes-permits  
**Direct PDF pattern:** None predictable.  
**Download behavior:** UPDES (Utah Pollutant Discharge Elimination System). Portal navigation required.

---

### VT — Vermont — DEC (VT)

**Agency:** Vermont Agency of Natural Resources, Department of Environmental Conservation  
**Portal:** https://dec.vermont.gov/watershed/wetlands/permit/  
**Permit search:** https://dec.vermont.gov/watershed/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### VA — Virginia — DEQ (VA)

**Agency:** Virginia Department of Environmental Quality  
**Portal:** https://www.deq.virginia.gov/programs/water/permits/vpdes  
**VPDES permit search:** https://www.deq.virginia.gov/programs/water/permits/vpdes  
**Direct PDF pattern:** None predictable.  
**Download behavior:** VPDES (Virginia Pollutant Discharge Elimination System). Portal navigation required.

---

### WA — Washington — Ecology

**Agency:** Washington State Department of Ecology  
**Portal:** https://ecology.wa.gov/water-shorelines/water-quality/water-permits  
**PARIS system:** https://apps.ecology.wa.gov/paris/  
**Direct PDF pattern:** None predictable from permit number alone.  
**Download behavior:** Ecology's PARIS (Permit and Reporting Information System) contains permit documents. Some permits posted as public PDFs on Ecology's website. Portal navigation preferred.

---

### WV — West Virginia — WVDEP

**Agency:** West Virginia Department of Environmental Protection  
**Portal:** https://dep.wv.gov/WWE/Programs/NPDES/Pages/default.aspx  
**Permit search:** https://dep.wv.gov/WWE/Programs/NPDES/Pages/default.aspx  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

### WI — Wisconsin — WDNR

**Agency:** Wisconsin Department of Natural Resources  
**Portal:** https://dnr.wisconsin.gov/topic/Permits/wpdes.html  
**eBusiness portal:** https://p.wi.gov/wdnr/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** WPDES (Wisconsin Pollutant Discharge Elimination System). Portal navigation required.

---

### WY — Wyoming — WDEQ

**Agency:** Wyoming Department of Environmental Quality, Water Quality Division  
**Portal:** https://deq.wyoming.gov/water-quality/npdes-updes/  
**Permit search:** https://deq.wyoming.gov/water-quality/npdes-updes/  
**Direct PDF pattern:** None predictable.  
**Download behavior:** Portal navigation required.

---

## Tips for Finding Permit Documents

- **Check ECHO document links first.** `Results.WebFireDocuments` and `Results.CAEDDocuments` in the DFR response sometimes contain direct PDF links already. This is the fastest path and works regardless of state.
- **Use Google to find direct PDFs.** `"[NPDES number]" filetype:pdf site:*.gov` often surfaces permit PDFs that state portals don't link prominently.
- **Check the utility or municipal website.** Capital improvement plans, annual reports, and rate studies often include permit summaries or process flow diagrams.
- **For process diagrams,** search for the permit application (Form 2A or state equivalent), which includes Unit Process Summary tables and flow diagrams. Google: `"[plant name]" "[city state]" "process flow diagram" filetype:pdf`
- **For inactive or legacy units,** engineering reports filed with permit applications are the best source. ECHO document links sometimes include these.
