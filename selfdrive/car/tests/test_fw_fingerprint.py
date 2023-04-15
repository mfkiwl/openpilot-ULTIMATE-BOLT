#!/usr/bin/env python3
import random
import time
import unittest
from collections import defaultdict
from parameterized import parameterized
import threading

from cereal import car
from common.params import Params
from selfdrive.car.car_helpers import interfaces
from selfdrive.car.fingerprints import FW_VERSIONS
from selfdrive.car.fw_versions import FW_QUERY_CONFIGS, VERSIONS, match_fw_to_car, get_fw_versions

CarFw = car.CarParams.CarFw
Ecu = car.CarParams.Ecu

ECU_NAME = {v: k for k, v in Ecu.schema.enumerants.items()}


class FakeSocket:
  def receive(self, non_blocking=False):
    pass

  def send(self, msg):
    pass


class TestFwFingerprint(unittest.TestCase):
  def assertFingerprints(self, candidates, expected):
    candidates = list(candidates)
    self.assertEqual(len(candidates), 1, f"got more than one candidate: {candidates}")
    self.assertEqual(candidates[0], expected)

  @parameterized.expand([(b, c, e[c]) for b, e in VERSIONS.items() for c in e])
  def test_fw_fingerprint(self, brand, car_model, ecus):
    CP = car.CarParams.new_message()
    for _ in range(200):
      fw = []
      for ecu, fw_versions in ecus.items():
        if not len(fw_versions):
          raise unittest.SkipTest("Car model has no FW versions")
        ecu_name, addr, sub_addr = ecu
        fw.append({"ecu": ecu_name, "fwVersion": random.choice(fw_versions), 'brand': brand,
                   "address": addr, "subAddress": 0 if sub_addr is None else sub_addr})
      CP.carFw = fw
      _, matches = match_fw_to_car(CP.carFw)
      self.assertFingerprints(matches, car_model)

  def test_no_duplicate_fw_versions(self):
    for car_model, ecus in FW_VERSIONS.items():
      with self.subTest(car_model=car_model):
        for ecu, ecu_fw in ecus.items():
          with self.subTest(ecu):
            duplicates = {fw for fw in ecu_fw if ecu_fw.count(fw) > 1}
            self.assertFalse(len(duplicates), f"{car_model}: Duplicate FW versions: Ecu.{ECU_NAME[ecu[0]]}, {duplicates}")

  def test_all_addrs_map_to_one_ecu(self):
    for brand, cars in VERSIONS.items():
      addr_to_ecu = defaultdict(set)
      for ecus in cars.values():
        for ecu_type, addr, sub_addr in ecus.keys():
          addr_to_ecu[(addr, sub_addr)].add(ecu_type)
          ecus_for_addr = addr_to_ecu[(addr, sub_addr)]
          ecu_strings = ", ".join([f'Ecu.{ECU_NAME[ecu]}' for ecu in ecus_for_addr])
          self.assertLessEqual(len(ecus_for_addr), 1, f"{brand} has multiple ECUs that map to one address: {ecu_strings} -> ({hex(addr)}, {sub_addr})")

  def test_data_collection_ecus(self):
    # Asserts no extra ECUs are in the fingerprinting database
    for brand, config in FW_QUERY_CONFIGS.items():
      for car_model, ecus in VERSIONS[brand].items():
        bad_ecus = set(ecus).intersection(config.extra_ecus)
        with self.subTest(car_model=car_model):
          self.assertFalse(len(bad_ecus), f'{car_model}: Fingerprints contain ECUs added for data collection: {bad_ecus}')

  def test_blacklisted_ecus(self):
    blacklisted_addrs = (0x7c4, 0x7d0)  # includes A/C ecu and an unknown ecu
    for car_model, ecus in FW_VERSIONS.items():
      with self.subTest(car_model=car_model):
        CP = interfaces[car_model][0].get_non_essential_params(car_model)
        if CP.carName == 'subaru':
          for ecu in ecus.keys():
            self.assertNotIn(ecu[1], blacklisted_addrs, f'{car_model}: Blacklisted ecu: (Ecu.{ECU_NAME[ecu[0]]}, {hex(ecu[1])})')

        elif CP.carName == "chrysler":
          # Some HD trucks have a combined TCM and ECM
          if CP.carFingerprint.startswith("RAM HD"):
            for ecu in ecus.keys():
              self.assertNotEqual(ecu[0], Ecu.transmission, f"{car_model}: Blacklisted ecu: (Ecu.{ECU_NAME[ecu[0]]}, {hex(ecu[1])})")

  def test_missing_versions_and_configs(self):
    brand_versions = set(VERSIONS.keys())
    brand_configs = set(FW_QUERY_CONFIGS.keys())
    if len(brand_configs - brand_versions):
      with self.subTest():
        self.fail(f"Brands do not implement FW_VERSIONS: {brand_configs - brand_versions}")

    if len(brand_versions - brand_configs):
      with self.subTest():
        self.fail(f"Brands do not implement FW_QUERY_CONFIG: {brand_versions - brand_configs}")

  def test_fw_request_ecu_whitelist(self):
    for brand, config in FW_QUERY_CONFIGS.items():
      with self.subTest(brand=brand):
        whitelisted_ecus = {ecu for r in config.requests for ecu in r.whitelist_ecus}
        brand_ecus = {fw[0] for car_fw in VERSIONS[brand].values() for fw in car_fw}
        brand_ecus |= {ecu[0] for ecu in config.extra_ecus}

        # each ecu in brand's fw versions + extra ecus needs to be whitelisted at least once
        ecus_not_whitelisted = brand_ecus - whitelisted_ecus

        ecu_strings = ", ".join([f'Ecu.{ECU_NAME[ecu]}' for ecu in ecus_not_whitelisted])
        self.assertFalse(len(whitelisted_ecus) and len(ecus_not_whitelisted),
                         f'{brand.title()}: FW query whitelist missing ecus: {ecu_strings}')


class TestFwFingerprintTiming(unittest.TestCase):
  def _benchmark(self, brand, num_pandas, ref_time, tol, n):
    params = Params()
    fake_socket = FakeSocket()

    times = []
    for _ in range(n):
      params.put_bool("ObdMultiplexingEnabled", True)
      thread = threading.Thread(target=get_fw_versions, args=(fake_socket, fake_socket, brand), kwargs=dict(num_pandas=num_pandas))
      thread.start()
      t = time.perf_counter()
      while thread.is_alive():
        time.sleep(0.02)
        if not params.get_bool("ObdMultiplexingChanged"):
          params.put_bool("ObdMultiplexingChanged", True)
      times.append(time.perf_counter() - t)

    avg_time = round(sum(times) / len(times), 2)
    self.assertLess(avg_time, ref_time + tol)
    self.assertGreater(avg_time, ref_time - tol, "Performance seems to have improved, update test refs.")
    return avg_time

  @parameterized.expand([(1,), (2,), ])
  def test_fw_query_timing(self, num_pandas):
    brand_ref_times = {
      1: {
        'body': 0.1,
        'chrysler': 0.3,
        'ford': 0.2,
        'honda': 0.5,
        'hyundai': 0.7,
        'mazda': 0.1,
        'nissan': 0.3,
        'subaru': 0.1,
        'tesla': 0.2,
        'toyota': 0.7,
        'volkswagen': 0.2,
      },
      2: {
        'hyundai': 1.1,
      }
    }

    for brand, config in FW_QUERY_CONFIGS.items():
      with self.subTest(brand=brand, num_pandas=num_pandas):
        multi_panda_requests = [r for r in config.requests if r.bus > 3]
        if not len(multi_panda_requests) and num_pandas > 1:
          raise unittest.SkipTest("No multi-panda FW queries")

        avg_time = self._benchmark(brand, num_pandas, brand_ref_times[num_pandas][brand], 0.1, 10)
        print(f'{brand=}, {num_pandas=}, {len(config.requests)=}, avg FW query time={avg_time} seconds')


if __name__ == "__main__":
  unittest.main()