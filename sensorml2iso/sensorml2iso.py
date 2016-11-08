import os
import errno
import io
import sys
from datetime import datetime, timedelta
from dateutil import parser
import pytz

# import urllib3
from urllib import quote, unquote, urlencode
from urlparse import urlparse
from collections import OrderedDict
from lxml import etree

# import numpy as np
import pandas as pd
from shapely.geometry import Point
import geopandas as gpd

from owslib.sos import SensorObservationService
from owslib.swe.sensor.sml import SensorML, Contact, Documentation
from owslib.util import testXMLValue, testXMLAttribute, nspath_eval
from owslib.namespaces import Namespaces

from pyoos.collectors.ioos.swe_sos import IoosSweSos
from pyoos.parsers.ioos.describe_sensor import IoosDescribeSensor
from pyoos.parsers.ioos.one.describe_sensor import ont

from jinja2 import Environment, PackageLoader


class Sensorml2Iso:
    """
    Attributes
    ----------
    service : str
        An IOOS DMAC-compliant SOS service to parse for active sensors to generate metadata for.
    active_station_days : int
        Number of days before present to designate stations as 'active' for inclusion/exclusion purposes.
    stations : str
        List of station URNs to filter by for processing
    getobs_req_hours : int
        Number of hours from last valid station observation time to use in GetObservation request example URLs
    output_dir : str
        Name of an output directory (relative) to output ISO 19115-2 XML metadata to
    more : str
        More class attributes...
    """

    def __init__(self, service=None, active_station_days=None, stations=None, getobs_req_hours=None, output_dir=None, verbose=False):
        """
        """

        self.service = service

        self.active_station_days = active_station_days
        self.stations = stations
        self.getobs_req_hours = getobs_req_hours
        self.verbose = verbose

        self.service_url = urlparse(self.service)
        self.server_name = self.service_url.netloc

        self.log = io.open('sensorml2iso.log', mode='wt', encoding='utf-8')

        if output_dir is not None:
            self.output_directory = output_dir
        else:
            self.output_directory = self.service_url.netloc
        self.create_output_dir()
        if self.verbose:
            try:
                # self.csv = io.open('sensorml2iso.csv', mode='wt', encoding='utf-8')
                self.csv = open('sensorml2iso.csv', mode='wt')
            except OSError:
                pass
            self.run_test()


    def run(self):
        """
        """
        self.namespaces = self.get_namespaces()
        print("Stations: {stations}".format(stations=self.stations))

        # obtain the stations DataFrame:
        stations_df = self.get_stations_df(self.service, self.stations)

        # Assign EPSG:4326 CRS, retrieved from epsg.io
        # The OGC WKT crs string is available directly at http://epsg.io/4326.wkt
        # or http://spatialreference.org/ref/epsg/4326/ogcwkt/
        crs = '''GEOGCS["WGS 84",
                   DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],
                     AUTHORITY["EPSG","6326"]],
                   PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],
                   UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],
                 AUTHORITY["EPSG","4326"]]'
        '''
        geometry = [Point(xy) for xy in zip(stations_df.lon, stations_df.lat)]
        self.stations_gdf = gpd.GeoDataFrame(stations_df, geometry=geometry, crs=crs)

        # determine active/inactive:
        station_active_date = datetime.now() - timedelta(days=self.active_station_days)
        active_cnt = len(stations_df[stations_df.ending > station_active_date.isoformat()])
        total_cnt = len(stations_df)
        print("'Active' stations: %d / Total stations: %d" % (active_cnt, total_cnt))

        # only relevant as an output in jupyter, no operation on stations_df:
        # stations_df[stations_df.ending > station_active_date.isoformat()]
        print("len(stations_df): {len:2.0f}".format(len=len(stations_df)))

        filtered_stations_df = stations_df.loc[stations_df.ending > station_active_date.isoformat()]
        print("len(filtered_stations_df): {len:2.0f}".format(len=len(filtered_stations_df)))

        # print(stations_df[stations_df.ending > station_active_date.isoformat()].to_csv().encode('utf-8'))
        if self.verbose:
            # self.csv.write(unicode(stations_df[stations_df.ending > station_active_date.isoformat()].to_csv(encoding='utf-8')))
            self.csv.write(stations_df[stations_df.ending > station_active_date.isoformat()].to_csv(encoding='utf-8'))

        self.generate_iso(stations_df)
        return

    # These functions are all from OWSLib, with minor adaptations
    def get_namespaces(self):
        """
        """
        n = Namespaces()
        namespaces = n.get_namespaces(["sml", "gml", "xlink", "swe"])
        namespaces["ism"] = "urn:us:gov:ic:ism:v2"
        return namespaces

    def nsp(self, path):
        """
        """
        return nspath_eval(path, self.namespaces)

    def get_stations_df(self, sos_url, station_urns_sel=None):
        """ Returns a GeoDataFrame
        """
        # LATER: ADD ERROR TEST/CATCH AFTER EACH WEB REQUEST
        oFrmt = 'text/xml; subtype="sensorML/1.0.1/profiles/ioos_sos/1.0"'
        params = {'service': 'SOS', 'request': 'GetCapabilities', 'acceptVersions': '1.0.0'}
        sos_url_params = sos_url + '?' + urlencode(params)
        sos_url_params_esc = sos_url_params.replace("&", "&amp;")
        #sos_url_params_quoted = quote(sos_url_params,"/=:")
        # sos_url_params_unquoted = unquote(sos_url_params)

        sosgc = SensorObservationService(sos_url_params)
        if station_urns_sel is not None:
            station_urns = station_urns_sel
        else:
            sos_collector = IoosSweSos(sos_url)
            station_urns = [urn.name for urn in sos_collector.server.offerings
                            if 'network' not in urn.name.split(':')]
            sos_collector.features = station_urns
            sml_lst = sos_collector.metadata(timeout=200)

        station_recs = []
        for station_idx, station_urn in enumerate(station_urns):
            if station_urns_sel is not None:
                sml_str = sosgc.describe_sensor(procedure=station_urn, outputFormat=oFrmt,
                                                timeout=200)
                sml = SensorML(sml_str)

            else:
                sml = sml_lst[station_idx]

            # debug:
            if self.verbose:
                self.log.write(unicode(etree.tostring(sml._root)))

            ds = IoosDescribeSensor(sml._root)

            pos = testXMLValue(ds.system.location.find(self.nsp('gml:Point/gml:pos')))

            system_el = sml._root.findall(self.nsp('sml:member'))[0].find(self.nsp('sml:System'))

            # Parse the DocumentList into a dict storing documents by index value 'name' (may cause index duplication
            # errors but there is not enough information in SensorML for alternatives)
            # Assume that member corresponds to xlink:arcrole="urn:ogc:def:role:webPage"
            documents = system_el.findall(self.nsp('sml:documentation/sml:DocumentList/sml:member'))
            documents_dct = {}
            for d in documents:
                document = Documentation(d)
                name = testXMLAttribute(d, "name")
                # url = document.documents[0].url
                documents_dct[name] = document

            contacts = system_el.findall(self.nsp('sml:contact/sml:ContactList/sml:member'))
            contacts_dct = {}
            for c in contacts:
                contact = Contact(c)
                role = contact.role.split('/')[-1]
                contacts_dct[role] = contact

            sweQuants = system_el.findall(self.nsp('sml:outputs/sml:OutputList/sml:output/swe:Quantity'))
            quant_lst = [sweQuant.attrib['definition'] for sweQuant in sweQuants]
            parameter_lst = [sweQuant.split('/')[-1] for sweQuant in quant_lst]

            beginPosition = testXMLValue(system_el.find(self.nsp('sml:validTime/gml:TimePeriod/gml:beginPosition')))
            begin_service_date = parser.parse(beginPosition)

            station = OrderedDict()
            station['station_urn'] = station_urn
            station['sos_url'] = sos_url_params_esc
            station['lon'] = float(pos.split()[1])
            station['lat'] = float(pos.split()[0])

            station['shortName'] = ds.shortName
            station['longName'] = ds.longName
            station['wmoID'] = ds.get_ioos_def('wmoID', 'identifier', ont)
            station['serverName'] = self.server_name

            # Some capabilities-level metadata:
            station['title'] = sosgc.identification.title
            station['abstract'] = sosgc.identification.abstract
            station['keywords'] = sosgc.identification.keywords
            station['begin_service_date'] = begin_service_date

            # Beware that a station can have >1 classifier of the same type
            # This code does not accommodate that possibility
            station['platformType'] = ds.platformType
            station['parentNetwork'] = ds.get_ioos_def('parentNetwork', 'classifier', ont)
            station['sponsor'] = ds.get_ioos_def('sponsor', 'classifier', ont)

            # store some nested dictionaries in 'station' for appopriate SensorML sources:
            station['contacts_dct'] = contacts_dct
            station['documents_dct'] = documents_dct

            # MW: the 'operator_' and 'publisher_' attributes can be removed bc they are not used
            # in the template code currently in favor of 'contacts_dct'
            station['operatorSector'] = ds.get_ioos_def('operatorSector', 'classifier', ont)
            station['operator_org'] = contacts_dct['operator'].organization
            station['operator_country'] = contacts_dct['operator'].country
            station['operator_url'] = contacts_dct['operator'].url
            station['operator_email'] = contacts_dct['operator'].email

            station['publisher'] = ds.get_ioos_def('publisher', 'classifier', ont)
            station['publisher_org'] = contacts_dct['publisher'].organization
            station['publisher_url'] = contacts_dct['publisher'].url
            # station_dct['publisher_email'] = contacts_dct['publisher'].electronicMailAddress

            station['starting'] = ds.starting
            station['ending'] = ds.ending
            station['starting_isostr'] = datetime.isoformat(ds.starting)
            station['ending_isostr'] = datetime.isoformat(ds.ending)

            station['parameter_uris'] = ','.join(quant_lst)
            station['parameters'] = ','.join(parameter_lst)
            station['variables'] = [var.split('/')[-1] for var in ds.variables]

            # create a dict to store parameters for valid example GetObservation requests for station:
            getobs_req_dct = {}

            # debug:
            if self.verbose:
                for var in ds.variables:
                    self.log.write(u"variable: {var}\n".format(var=var))
                    print("variable: {var}".format(var=var))

            # print(sosgc.contents)
            # for id, offering in sosgc.contents.iteritems():
            #    print("sosgc.contents: {item}".format(item=id))

            # response_formats = sosgc.contents[station_urn].response_formats
            response_formats = []
            for id, sosgc.content in sosgc.contents.items():
                if sosgc.content.name == station_urn:
                    response_formats = sosgc.content.response_formats
            # response_formats = [ sosgc.content.response_formats for id, sosgc.content in sosgc.contents.items() if sosgc.content.name == station_urn ]

            if self.verbose:
                for format in response_formats:
                    self.log.write(u"responseFormat: {format}\n".format(format=format))
                    print("responseFormat: {format}".format(format=format))

            event_time = "{end:%Y-%m-%dT%H:%M:%S%z}/{begin:%Y-%m-%dT%H:%M:%S%z}\n".format(end=ds.ending, begin=ds.ending - timedelta(hours=self.getobs_req_hours))
            if self.verbose:
                self.log.write(u"eventTime: {time}\n".format(time=event_time))
                print("eventTime: {time}".format(time=event_time))

            # getobs_params_base = {'service': 'SOS', 'request': 'GetObservation', 'version': '1.0.0', 'offering': station_urn, 'responseFormat': 'application/json'}
            getobs_params_base = {'service': 'SOS', 'request': 'GetObservation', 'version': '1.0.0', 'offering': station_urn, 'eventTime': event_time, 'responseFormat': response_formats[-1]}

            for variable in ds.variables:
                getobs_params = getobs_params_base.copy()
                getobs_params['observedProperty'] = variable
                getobs_request_url = sos_url + '?' + quote(urlencode(getobs_params))
                getobs_request_url_unencoded = unquote(getobs_request_url).replace("&", "&amp;")
                getobs_req_dct[variable] = getobs_request_url_unencoded
                if self.verbose:
                    self.log.write(u"\n\ngetobs_request_url (var: {variable}): {getobs_request_url},\ngetobs_request_url_unencoded (var: {variable}): {getobs_request_url_unencoded}".format(variable=variable.split("/")[-1], getobs_request_url=getobs_request_url, getobs_request_url_unencoded=getobs_request_url_unencoded))
                    print("\n\ngetobs_request_url (var: {variable}): {getobs_request_url},\ngetobs_request_url_unencoded (var: {variable}): {getobs_request_url_unencoded}".format(variable=variable.split("/")[-1], getobs_request_url=getobs_request_url, getobs_request_url_unencoded=getobs_request_url_unencoded))

            # debug:
            now = datetime.now(pytz.utc)
            then = now - timedelta(hours=self.getobs_req_hours)
            if self.verbose:
                self.log.write(u"now: {now:%Y-%m-%dT%H:%M:%S%z}, then: {then:%Y-%m-%dT%H:%M:%S%z}\n".format(now=now, then=then))
                print("now: {now:%Y-%m-%dT%H:%M:%S%z}, then: {then:%Y-%m-%dT%H:%M:%S%z}".format(now=now, then=then))

            station['getobs_req_dct'] = getobs_req_dct

            station_recs.append(station)

        stations_df = pd.DataFrame.from_records(station_recs, columns=station.keys())
        stations_df.index = stations_df['station_urn']

        return stations_df

    def generate_iso(self, df):
        """
        """

        # set up the Jinja2 template:
        env = Environment(loader=PackageLoader('sensorml2iso', 'templates'), trim_blocks=True, lstrip_blocks=True)
        template = env.get_template('sensorml_iso.xml')

        for idx, station in df.iterrows():
            ctx = {}
            # populate some general elements for the template:
            # we can use format filters in the template to format dates...
            # ctx['metadataDate'] = "{metadata_date:%Y-%m-%d}".format(metadata_date=datetime.today())
            ctx['metadataDate'] = datetime.now()

            # debug: get the first station:
            # station = df.iloc[0]

            ctx['identifier'] = station.station_urn
            ctx['contacts_dct'] = station['contacts_dct']
            ctx['documents_dct'] = station['documents_dct']

            ctx['sos_url'] = station['sos_url']
            ctx['lon'] = station['lon']
            ctx['lat'] = station['lat']
            ctx['shortName'] = station.shortName
            ctx['longName'] = station.longName
            ctx['wmoID'] = station.wmoID
            ctx['serverName'] = station.serverName

            ctx['title'] = station.title
            ctx['abstract'] = station.abstract
            ctx['keywords'] = station.keywords
            ctx['beginServiceDate'] = station.begin_service_date

            ctx['platformType'] = station.platformType
            ctx['parentNetwork'] = station.parentNetwork
            ctx['sponsor'] = station.sponsor

            ctx['starting'] = station.starting
            ctx['ending'] = station.ending

            ctx['parameter_uris'] = station.parameter_uris
            ctx['parameters'] = station.parameter_uris
            ctx['variables'] = station.variables

            ctx['getobs_req_dct'] = station.getobs_req_dct

            output_filename = os.path.join(self.output_directory, "{serverName}-{station}.xml".format(serverName=self.server_name, station=station.station_urn.replace(":", "_")))
            try:
                iso_xml = template.render(ctx)
                output_file = io.open(output_filename, mode='wt', encoding='utf8')
                output_file.write(iso_xml)
                output_file.close()
                if self.verbose:
                    self.log.write(u"\nMetadata for station: {station} written to output file: {out_file}\n".format(station=station.station_urn, out_file=os.path.abspath(output_filename)))
                    self.log.write(iso_xml)
                    print("\nMetadata for station: {station} written to output file: {out_file}".format(station=station.station_urn, out_file=os.path.abspath(output_filename)))
                    print(iso_xml)
            except OSError as ex:
                if ex.errno == errno.EEXIST:
                    self.log.write(u"Output file: {out_file} already exists, skipping.\n".format(out_file=output_filename))
                    print("Output file: {out_file} already exists, skipping.".format(out_file=output_filename))
                    # sys.exit("Error: the output directory passed: {output_dir} already exists.".format(output_dir=os.path.abspath(self.output_directory)))
                else:
                    sys.exit("Error: Unable to open output file: {out_file} for writing, aborting.".format(out_file=output_filename))

    def create_output_dir(self):
        """
        """
        try:
            os.makedirs(self.output_directory)
            # raise OSError
        except OSError as ex:
            if ex.errno == errno.EEXIST and os.path.isdir(self.output_directory):
                self.log.write(u"Error: the output directory passed: {output_dir} already exists.\n".format(output_dir=os.path.abspath(self.output_directory)))
                sys.exit("Error: the output directory passed: {output_dir} already exists.".format(output_dir=os.path.abspath(self.output_directory)))
            else:
                self.log.write(u"Error: the output directory passed: {output_dir} was not able to be created.\n".format(output_dir=os.path.abspath(self.output_directory)))
                sys.exit("Error: the output directory passed: {output_dir} was not able to be created.".format(output_dir=os.path.abspath(self.output_directory)))

    def run_test(self):
        """
        """
        print("Service: {service}, verbose: {verbose}, o: {o}".format(service=self.service, verbose=self.verbose, o=self.output_directory))
        active_date = datetime.now() - timedelta(days=self.active_station_days)

        # print("Date for determining active/inactive stations in SOS service: {active_date}".format(active_date=active_date.strftime("%Y-%m-%d")))
        print("Date for determining active/inactive stations in SOS service: {active_date:%Y-%m-%d}".format(active_date=active_date))
