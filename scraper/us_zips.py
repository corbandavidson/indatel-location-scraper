"""
Curated US zip code grid for nationwide coverage of search-based locators.

This list aims to put a search point within ~50–100 miles of every populated
area in the US. We weight toward major metros (where most chain stores live)
but include rural/state-spanning fillers so single-store-per-state chains
aren't missed.

For chains with ~15,000 locations (e.g. Starbucks), sweeping all of these
yields tens of thousands of API responses; the deduper collapses by store ID
or address+zip to produce the final list.

Source: US Census, plus state geographic centers for rural coverage.
"""

# Major metros — covers ~85% of chain store density
US_METRO_ZIPS = [
    # Northeast
    "10001", "10002", "10025",  # NYC
    "11201", "11215",            # Brooklyn
    "02108", "02115", "02134",  # Boston
    "19103", "19146",            # Philadelphia
    "20001", "20007", "20016",  # DC
    "21201", "21218",            # Baltimore
    "07102", "07302",            # Newark / Jersey City
    "06103", "06511",            # Hartford / New Haven CT
    "02903", "02906",            # Providence
    "03101", "03301",            # Manchester / Concord NH
    "04101", "04330",            # Portland / Augusta ME
    "05401", "05601",            # Burlington / Montpelier VT
    "12207", "12180",            # Albany / Troy
    "14202", "14604",            # Buffalo / Rochester
    "13202", "13502",            # Syracuse / Utica
    "15222", "15213",            # Pittsburgh
    "16501", "17101",            # Erie / Harrisburg
    "18102", "18503",            # Allentown / Scranton

    # Southeast
    "30303", "30309",            # Atlanta
    "30901",                      # Augusta GA
    "31401",                      # Savannah
    "33101", "33125", "33139",   # Miami
    "32801", "32803",            # Orlando
    "33602", "33609",            # Tampa
    "32202",                      # Jacksonville
    "32301",                      # Tallahassee
    "33401",                      # West Palm Beach
    "32501",                      # Pensacola
    "34102",                      # Naples
    "28202", "28209",            # Charlotte
    "27601", "27606",            # Raleigh
    "27401",                      # Greensboro
    "28801",                      # Asheville
    "27834",                      # Greenville NC
    "29201", "29401",            # Columbia / Charleston SC
    "29601",                      # Greenville SC
    "29501",                      # Florence SC
    "23219", "23510",            # Richmond / Norfolk
    "22301", "22102",            # Alexandria / Tysons
    "24016", "24501",            # Roanoke / Lynchburg
    "25301",                      # Charleston WV
    "26505",                      # Morgantown
    "37201", "37203",            # Nashville
    "38103",                      # Memphis
    "37402",                      # Chattanooga
    "37902",                      # Knoxville
    "40202", "40203",            # Louisville
    "40507",                      # Lexington
    "35203", "35242",            # Birmingham
    "36104",                      # Montgomery
    "36602",                      # Mobile
    "35801",                      # Huntsville
    "39201",                      # Jackson MS
    "39501",                      # Gulfport
    "39530",                      # Biloxi
    "32503",                      # Pensacola

    # Midwest
    "60601", "60611", "60616", "60622", "60630",  # Chicago
    "61602",                      # Peoria
    "62701",                      # Springfield IL
    "61101",                      # Rockford
    "46204", "46220",            # Indianapolis
    "47708",                      # Evansville
    "46802",                      # Fort Wayne
    "46601",                      # South Bend
    "44113", "44115",            # Cleveland
    "45202", "45219",            # Cincinnati
    "43215", "43210",            # Columbus
    "44503",                      # Youngstown
    "45402",                      # Dayton
    "43604",                      # Toledo
    "48201", "48226",            # Detroit
    "49503",                      # Grand Rapids
    "48933",                      # Lansing
    "48073",                      # Royal Oak
    "49684",                      # Traverse City
    "49855",                      # Marquette
    "53202", "53203",            # Milwaukee
    "53703",                      # Madison
    "54301",                      # Green Bay
    "53081",                      # Sheboygan
    "55401", "55402",            # Minneapolis
    "55101",                      # St Paul
    "55802",                      # Duluth
    "56301",                      # St Cloud
    "55901",                      # Rochester MN
    "50309",                      # Des Moines
    "52401",                      # Cedar Rapids
    "52801",                      # Davenport
    "51101",                      # Sioux City IA
    "63101", "63103", "63108",   # St Louis
    "64108",                      # Kansas City MO
    "65101",                      # Jefferson City
    "65802",                      # Springfield MO
    "66101",                      # Kansas City KS
    "66603",                      # Topeka
    "67202",                      # Wichita
    "68102",                      # Omaha
    "68508",                      # Lincoln
    "57104",                      # Sioux Falls
    "57701",                      # Rapid City
    "58102",                      # Fargo
    "58501",                      # Bismarck
    "58201",                      # Grand Forks

    # South Central
    "75201", "75204", "75219",   # Dallas
    "76102",                      # Fort Worth
    "77002", "77005", "77019",   # Houston
    "78205", "78215",            # San Antonio
    "78701", "78704",            # Austin
    "79901",                      # El Paso
    "79401",                      # Lubbock
    "76701",                      # Waco
    "78401",                      # Corpus Christi
    "78550",                      # Harlingen
    "75701",                      # Tyler
    "77550",                      # Galveston
    "76301",                      # Wichita Falls
    "79601",                      # Abilene
    "73102", "73104",            # Oklahoma City
    "74103",                      # Tulsa
    "73401",                      # Ardmore
    "72201",                      # Little Rock
    "72701",                      # Fayetteville AR
    "70112", "70118",            # New Orleans
    "70802",                      # Baton Rouge
    "71101",                      # Shreveport

    # Mountain West
    "80202", "80206",            # Denver
    "80903",                      # Colorado Springs
    "80401",                      # Golden / Lakewood
    "81501",                      # Grand Junction
    "81601",                      # Glenwood Springs
    "85003", "85016", "85251",   # Phoenix
    "85701", "85705",            # Tucson
    "86001",                      # Flagstaff
    "87102",                      # Albuquerque
    "87501",                      # Santa Fe
    "88001",                      # Las Cruces
    "84101", "84102",            # Salt Lake City
    "84601",                      # Provo
    "84770",                      # St George
    "83702",                      # Boise
    "83864",                      # Sandpoint
    "83201",                      # Pocatello
    "59601", "59101",            # Helena / Billings
    "59802",                      # Missoula
    "59401",                      # Great Falls
    "82001",                      # Cheyenne
    "82601",                      # Casper
    "83001",                      # Jackson WY
    "89101", "89109",            # Las Vegas
    "89501",                      # Reno

    # West Coast
    "90001", "90014", "90025", "90034",  # Los Angeles
    "92101", "92108",            # San Diego
    "94102", "94110", "94117",   # San Francisco
    "94612",                      # Oakland
    "95113",                      # San Jose
    "95814",                      # Sacramento
    "93701",                      # Fresno
    "93301",                      # Bakersfield
    "92501",                      # Riverside
    "92805",                      # Anaheim
    "93401",                      # San Luis Obispo
    "93101",                      # Santa Barbara
    "95501",                      # Eureka
    "96001",                      # Redding
    "97201", "97214", "97232",   # Portland OR
    "97301",                      # Salem
    "97401",                      # Eugene
    "97601",                      # Klamath Falls
    "97701",                      # Bend
    "97501",                      # Medford
    "98101", "98104", "98109",   # Seattle
    "98401",                      # Tacoma
    "99201",                      # Spokane
    "98660",                      # Vancouver WA
    "98801",                      # Wenatchee
    "98225",                      # Bellingham

    # Alaska & Hawaii (limited but covered)
    "99501", "99701",            # Anchorage / Fairbanks
    "96813", "96817", "96720",   # Honolulu / Hilo
]


# Smaller cities + rural fillers so single-store-per-state chains aren't missed
US_RURAL_FILL_ZIPS = [
    "04401",  # Bangor ME
    "03801",  # Portsmouth NH
    "05701",  # Rutland VT
    "01060",  # Northampton MA
    "01103",  # Springfield MA
    "06320",  # New London CT
    "08401",  # Atlantic City NJ
    "17601",  # Lancaster PA
    "17701",  # Williamsport PA
    "19958",  # Lewes DE
    "21801",  # Salisbury MD
    "22601",  # Winchester VA
    "23606",  # Newport News VA
    "24201",  # Bristol VA
    "26101",  # Parkersburg WV
    "27889",  # Washington NC
    "28557",  # Morehead City NC
    "29577",  # Myrtle Beach
    "31601",  # Valdosta GA
    "32301",  # Tallahassee
    "33701",  # St Petersburg FL
    "34601",  # Brooksville FL
    "35630",  # Florence AL
    "36801",  # Auburn AL
    "37601",  # Johnson City TN
    "38301",  # Jackson TN
    "39501",  # Gulfport MS
    "40601",  # Frankfort KY
    "41501",  # Pikeville KY
    "42101",  # Bowling Green KY
    "43055",  # Newark OH
    "44505",  # Youngstown OH
    "45601",  # Chillicothe OH
    "46514",  # Elkhart IN
    "47331",  # Connersville IN
    "48060",  # Port Huron MI
    "48858",  # Mount Pleasant MI
    "49770",  # Petoskey MI
    "53715",  # Madison WI
    "54501",  # Rhinelander WI
    "55811",  # Duluth MN
    "57401",  # Aberdeen SD
    "58301",  # Devils Lake ND
    "59044",  # Laurel MT
    "59102",  # Billings
    "60901",  # Kankakee IL
    "61820",  # Champaign IL
    "62901",  # Carbondale IL
    "63501",  # Kirksville MO
    "64801",  # Joplin MO
    "65801",  # Springfield MO area
    "66801",  # Emporia KS
    "67401",  # Salina KS
    "68801",  # Grand Island NE
    "69101",  # North Platte NE
    "70501",  # Lafayette LA
    "71201",  # Monroe LA
    "72401",  # Jonesboro AR
    "73501",  # Lawton OK
    "74401",  # Muskogee OK
    "75901",  # Lufkin TX
    "76301",  # Wichita Falls TX
    "78840",  # Del Rio TX
    "79101",  # Amarillo TX
    "80501",  # Longmont CO
    "81001",  # Pueblo CO
    "82601",  # Casper WY
    "82801",  # Sheridan WY
    "83201",  # Pocatello ID
    "83501",  # Lewiston ID
    "84601",  # Provo UT
    "85364",  # Yuma AZ
    "86301",  # Prescott AZ
    "87301",  # Gallup NM
    "88201",  # Roswell NM
    "89701",  # Carson City NV
    "89801",  # Elko NV
    "90802",  # Long Beach
    "91101",  # Pasadena
    "92277",  # Twentynine Palms
    "93720",  # Fresno
    "94503",  # Napa
    "95350",  # Modesto
    "95821",  # Sacramento
    "96080",  # Red Bluff CA
    "97123",  # Hillsboro OR
    "97520",  # Ashland OR
    "98201",  # Everett WA
    "98501",  # Olympia WA
    "98926",  # Ellensburg WA
    "99362",  # Walla Walla WA
]


# Combined list — order doesn't matter; dedup happens later by store ID/address.
US_ZIPS = US_METRO_ZIPS + US_RURAL_FILL_ZIPS


def get_us_zips(comprehensive: bool = True) -> list[str]:
    """
    Return the curated US zip code grid.

    comprehensive=True returns the full ~300 zip grid for nationwide coverage.
    comprehensive=False returns a tiny 5-zip sample for quick smoke tests.
    """
    if not comprehensive:
        return ["60601", "10001", "90001", "77001", "85001"]
    return US_ZIPS
