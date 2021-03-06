# Copyright (C) 2014, CERN
# This software is distributed under the terms of the GNU General Public
# Licence version 3 (GPL Version 3), copied verbatim in the file "COPYING".
# In applying this license, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as Intergovernmental Organization
# or submit itself to any jurisdiction.

import os
import logging
import yaml
import shutil
import re
import math

from jens.git import hash_object
from jens.decorators import timed
from jens.errors import JensEnvironmentsError
from jens.tools import refname_to_dirname
from jens.tools import aggregate_deltas

DIRECTORY_ENVIRONMENTS_CONF_FILENAME = "environment.conf"

@timed
def refresh_environments(settings, lock, repositories_deltas, inventory):
    logging.debug("Calculating delta...")
    delta = _calculate_delta(settings)
    logging.info("New environments: %s" % delta['new'])
    logging.info("Existing and changed environments: %s" % delta['changed'])
    logging.debug("Existing but not changed environments: %s" % delta['notchanged'])
    logging.info("Deleted environments: %s" % delta['deleted'])

    # Based on monitoring data, time will determine if it's ok or not
    total_new, total_deleted = aggregate_deltas(repositories_deltas)
    lock.renew(int(math.ceil((len(delta['new']) * 0.3) +
       (len(delta['changed']) * 0.4) +
       (len(delta['notchanged']) * (0.2*total_new + 0.1*total_deleted)) +
       (len(delta['deleted']) * 0.1))) + 2)

    logging.info("Creating new environments...")
    _create_new_environments(settings, delta['new'], inventory)
    logging.info("Purging deleted environments...")
    _purge_deleted_environments(settings, delta['deleted'])
    logging.info("Recreating changed environments...")
    _recreate_changed_environments(settings, delta['changed'], inventory)
    logging.info("Refreshing not changed environments...")
    _refresh_notchanged_environments(settings, delta['notchanged'],
        repositories_deltas)

def _refresh_notchanged_environments(settings, environments, repositories_deltas):
    for environment in environments:
        logging.debug("Refreshing environment '%s'..." % environment)
        try:
            definition = read_environment_definition(settings, environment)
        except JensEnvironmentsError, error:
            logging.error("Unable to read and parse '%s' definition (%s). Skipping" % \
                    (environment, error))
            return

        if definition.get('default', None) is None:
            logging.debug("Environment '%s' won't get new modules (no default)" % environment)
        else:
            for module in repositories_deltas['modules']['new']:
                try:
                    _link_module(settings, module, environment, definition)
                except JensEnvironmentsError, error:
                    logging.error("Failed to link module '%s' in enviroment '%s' (%s)" % \
                        (module, environment, error))

        for module in repositories_deltas['modules']['deleted']:
            logging.debug("Deleting module '%s' from environment '%s'" %
                (module, environment))
            _unlink_module(settings, module, environment)

        if definition.get('default', None) is None:
            logging.debug("Environment '%s' won't get new hostgroups (no default)" % environment)
        else:
            for hostgroup in repositories_deltas['hostgroups']['new']:
                try:
                    _link_hostgroup(settings, hostgroup, environment, definition)
                except JensEnvironmentsError, error:
                    logging.error("Failed to link hostgroup '%s' in enviroment '%s' (%s)" % \
                        (hostgroup, environment, error))

        for hostgroup in repositories_deltas['hostgroups']['deleted']:
            logging.debug("Deleting hostgroup '%s' from environment '%s'" %
                (hostgroup, environment))
            _unlink_hostgroup(settings, hostgroup, environment)

def _recreate_changed_environments(settings, environments, inventory):
    for environment in environments:
        logging.info("Recreating environment '%s'" % environment)
        _purge_deleted_environment(settings, environment)
        _create_new_environment(settings, environment, inventory)

def _purge_deleted_environments(settings, environments):
    for environment in environments:
        _purge_deleted_environment(settings, environment)

def _purge_deleted_environment(settings, environment):
    logging.info("Deleting environment '%s'" % environment)
    env_basepath = "%s/%s" % (settings.ENVIRONMENTSDIR, environment)
    shutil.rmtree(env_basepath)
    logging.info("Deleted '%s'" % env_basepath)
    _remove_environment_annotation(settings, environment)

def _create_new_environments(settings, environments, inventory):
    for environment in environments:
        _create_new_environment(settings, environment, inventory)

def _create_new_environment(settings, environment, inventory):
    logging.info("Creating new environment '%s'" % environment)

    if re.match(r"^\w+$", environment) is None:
        logging.error("Environment name '%s' is invalid. Skipping" % environment)
        return

    try:
        definition = read_environment_definition(settings, environment)
    except JensEnvironmentsError, error:
        logging.error("Unable to read and parse '%s' definition (%s). Skipping" % \
            (environment, error))
        return

    if definition is None:
        logging.error("Environment '%s' is empty" % environment)
        return

    logging.debug("Creating directory structure...")
    env_basepath = "%s/%s" % (settings.ENVIRONMENTSDIR, environment)
    os.mkdir(env_basepath)
    for directory in ("modules", "hostgroups", "hieradata"):
        os.mkdir("%s/%s" % (env_basepath, directory))

    hieradata_directories = ("module_names", "hostgroups", "fqdns")
    for directory in hieradata_directories:
        os.mkdir("%s/hieradata/%s" % (env_basepath, directory))

    logging.info("Processing modules...")
    modules = inventory['modules'].keys()
    if not 'default' in definition:
        try:
            necessary_modules = definition['overrides']['modules'].keys()
        except KeyError:
            necessary_modules = set()
        modules = set(modules).intersection(necessary_modules)
    for module in modules:
        try:
            _link_module(settings, module, environment, definition)
        except JensEnvironmentsError, error:
            logging.error("Failed to link module '%s' in enviroment '%s' (%s)" % \
                (module, environment, error))

    logging.info("Processing hostgroups...")
    hostgroups = inventory['hostgroups'].keys()
    if not 'default' in definition:
        try:
            necessary_hostgroups = definition['overrides']['hostgroups'].keys()
        except KeyError:
            necessary_hostgroups = set()
        hostgroups = set(hostgroups).intersection(necessary_hostgroups)
    for hostgroup in hostgroups:
        try:
            _link_hostgroup(settings, hostgroup, environment, definition)
        except JensEnvironmentsError, error:
            logging.error("Failed to link hostgroup '%s' in enviroment '%s' (%s)" % \
                (hostgroup, environment, error))

    logging.info("Processing site...")
    try:
        _link_site(settings, environment, definition)
    except JensEnvironmentsError, error:
        logging.error("Failed to link site in enviroment '%s' (%s)" % \
            (environment, error))

    logging.info("Processing common Hiera data...")
    try:
        _link_common_hieradata(settings, environment, definition)
    except JensEnvironmentsError, error:
        logging.error("Failed to link common hieradata in enviroment '%s' (%s)" % \
            (environment, error))

    if settings.DIRECTORY_ENVIRONMENTS:
        try:
            _add_configuration_file(settings, environment)
        except JensEnvironmentsError, error:
            logging.error("Failed to generate config file for environment '%s' (%s)" % \
                (environment, error))

    _annotate_environment(settings, environment)

def read_environment_definition(settings, environment):
    try:
        path = settings.ENV_METADATADIR + "/%s.yaml" % environment
        logging.debug("Reading environment from %s" % path)
        environment = yaml.load(open(path, 'r'))
        for key in ('notifications',):
            if key not in environment:
                raise JensEnvironmentsError("Missing '%s' in environemnt '%s'" %
                    (key, environment))
        if 'overrides' in environment and environment['overrides'] is None:
                raise JensEnvironmentsError("Lacking overrides in environment '%s'" %
                    environment)
        # What about checking that default in settings.mandatory_barnches?
        return environment
    except yaml.YAMLError:
        raise JensEnvironmentsError("Unable to parse %s" % path)
    except IOError:
        raise JensEnvironmentsError("Unable to open %s for reading" % path)

def _link_module(settings, module, environment, definition):
    branch, overridden = _resolve_branch(settings, 'modules', module, definition)
    logging.debug("Adding module '%s' (%s) to environment '%s'" %
        (module, branch, environment))

    # 1. Module's code directory
    # LINK_NAME: $environment/modules/$module
    # TARGET: $clonedir/modules/$module/$branch/code
    target = "%s/modules/%s/%s/code" % \
        (settings.CLONEDIR, module, branch)
    link_name = _generate_module_env_code_path(settings,
        module, environment)
    target = os.path.relpath(target,
        os.path.abspath(os.path.join(link_name, os.pardir)))
    logging.debug("Linking %s to %s" % (link_name, target))
    try:
        os.symlink(target, link_name)
    except OSError, error:
        raise JensEnvironmentsError(error)

    # 2. Module's data directory
    # LINK_NAME: $environment/hieradata/module_names/$module
    # TARGET: $clonedir/modules/$module/$branch/data
    target = "%s/modules/%s/%s/data" % \
        (settings.CLONEDIR, module, branch)
    link_name = _generate_module_env_hieradata_path(settings,
        module, environment)
    target = os.path.relpath(target, \
        os.path.abspath(os.path.join(link_name, os.pardir)))
    logging.debug("Linking %s to %s" % (link_name, target))
    try:
        os.symlink(target, link_name)
    except OSError, error:
        raise JensEnvironmentsError(error)

def _link_hostgroup(settings, hostgroup, environment, definition):
    branch, overridden = _resolve_branch(settings, 'hostgroups', hostgroup, definition)
    logging.debug("Adding hostgroup '%s' (%s) to environment '%s'" %
        (hostgroup, branch, environment))
    # 1. Hostgroup's code directory
    # LINK_NAME: $environment/hostgroups/hg_$hostgroup
    # TARGET: $clonedir/hostgroups/$hostgroup/$branch/code
    target = "%s/hostgroups/%s/%s/code" % \
        (settings.CLONEDIR, hostgroup, branch)
    link_name = _generate_hostgroup_env_code_path(settings,
        hostgroup, environment)
    target = os.path.relpath(target,
        os.path.abspath(os.path.join(link_name, os.pardir)))
    logging.debug("Linking %s to %s" % (link_name, target))
    try:
        os.symlink(target, link_name)
    except OSError, error:
        raise JensEnvironmentsError(error)

    # 2. Hostgroup's hostgroup data directory
    # LINK_NAME: $environment/hostgroups/hieratata/hostgroups/$hostgroup
    # TARGET: $clonedir/hostgroups/$hostgroup/$branch/data/hostgroup
    target = "%s/hostgroups/%s/%s/data/hostgroup" % \
        (settings.CLONEDIR, hostgroup, branch)
    link_name = \
        _generate_hostgroup_env_hieradata_hostgroup_path(
        settings, hostgroup, environment)
    target = os.path.relpath(target, \
        os.path.abspath(os.path.join(link_name, os.pardir)))
    logging.debug("Linking %s to %s" % (link_name, target))
    try:
        os.symlink(target, link_name)
    except OSError, error:
        raise JensEnvironmentsError(error)

    # 3. Hostgroup's FQDNs data directory
    # LINK_NAME: $environment/hostgroups/hieratata/fqdns/$hostgroup
    # TARGET: $clonedir/hostgroups/$hostgroup/$branch/data/fqdns
    target = "%s/hostgroups/%s/%s/data/fqdns" % \
        (settings.CLONEDIR, hostgroup, branch)
    link_name = \
        _generate_hostgroup_env_hieradata_fqdns_path(
        settings, hostgroup, environment)
    target = os.path.relpath(target, \
        os.path.abspath(os.path.join(link_name, os.pardir)))
    logging.debug("Linking %s to %s" % (link_name, target))
    try:
        os.symlink(target, link_name)
    except OSError, error:
        raise JensEnvironmentsError(error)

def _unlink_module(settings, module, environment):
    # 1. Module's code directory
    # LINK_NAME: $environment/modules/$module
    link_name = _generate_module_env_code_path(settings,
        module, environment)
    logging.debug("Making sure link '%s' does not exist" % link_name)
    if os.path.islink(link_name):
        os.unlink(link_name)

    # 2. Module's data directory
    # LINK_NAME: $environment/hieradata/module_names/$module
    link_name = _generate_module_env_hieradata_path(settings,
        module, environment)
    logging.debug("Making sure link '%s' does not exist" % link_name)
    if os.path.islink(link_name):
        os.unlink(link_name)

def _unlink_hostgroup(settings, hostgroup, environment):
    # 1. Hostgroup's code directory
    # LINK_NAME: $environment/hostgroups/hg_$hostgroup
    link_name = _generate_hostgroup_env_code_path(settings,
        hostgroup, environment)
    logging.debug("Making sure link '%s' does not exist" % link_name)
    if os.path.islink(link_name):
        os.unlink(link_name)

    # 2. Hostgroup's hostgroup data directory
    # LINK_NAME: $environment/hostgroups/hieratata/hostgroups/$hostgroup
    link_name = \
        _generate_hostgroup_env_hieradata_hostgroup_path(
        settings, hostgroup, environment)
    logging.debug("Making sure link '%s' does not exist" % link_name)
    if os.path.islink(link_name):
        os.unlink(link_name)

    # 3. Hostgroup's FQDNs data directory
    # LINK_NAME: $environment/hostgroups/hieratata/fqdns/$hostgroup
    link_name = \
        _generate_hostgroup_env_hieradata_fqdns_path(
        settings, hostgroup, environment)
    logging.debug("Making sure link '%s' does not exist" % link_name)
    if os.path.islink(link_name):
        os.unlink(link_name)

def _generate_module_env_code_path(settings, module, environment):
    return "%s/%s/modules/%s" % \
        (settings.ENVIRONMENTSDIR, environment, module)

def _generate_module_env_hieradata_path(settings, module, environment):
    return "%s/%s/hieradata/module_names/%s" % \
        (settings.ENVIRONMENTSDIR, environment, module)

def _generate_hostgroup_env_code_path(settings, hostgroup, environment):
    return "%s/%s/hostgroups/hg_%s" % \
        (settings.ENVIRONMENTSDIR, environment, hostgroup)

def _generate_hostgroup_env_hieradata_hostgroup_path(settings, hostgroup, environment):
    return "%s/%s/hieradata/hostgroups/%s" % \
        (settings.ENVIRONMENTSDIR, environment, hostgroup)

def _generate_hostgroup_env_hieradata_fqdns_path(settings, hostgroup, environment):
    return "%s/%s/hieradata/fqdns/%s" % \
        (settings.ENVIRONMENTSDIR, environment, hostgroup)

def _annotate_environment(settings, environment):
    hash_cache_file = open(settings.CACHEDIR + "/environments/%s" % \
            environment, "w+")
    environment_definition = settings.ENV_METADATADIR + "/%s.yaml" % \
            environment
    hash_value = hash_object(environment_definition)
    logging.debug("New cached hash for environment '%s' is '%s'" % \
        (environment, hash_value))
    # TODO: Add error handling here, if the cache can't be saved
    # basically the environment will be regenerated in the next
    # run (which is fine, but must be logged at INFO level)
    hash_cache_file.write(hash_value)
    hash_cache_file.close()

def _remove_environment_annotation(settings, environment):
    logging.debug("Removing cached hash for environment '%s'" % environment)
    hash_cache_file = settings.CACHEDIR + "/environments/%s" % environment
    try:
        os.remove(hash_cache_file)
    # This shouldn't ever happen unless someone deleted the file or
    # changed its permissions externally
    except OSError, error:
        logging.error("Couldn't remove cached hash for environemnt '%s'" %
            environment)

def get_names_of_declared_environments(settings):
    environments = os.listdir(settings.ENV_METADATADIR)
    environments = filter(lambda x: re.match("^.+?\.yaml$", x), environments)
    return map(lambda x: re.sub("\.yaml$", "", x), environments)

def _calculate_delta(settings):
    delta = {'notchanged': [], 'changed': []}
    current_envs = set(os.listdir(settings.CACHEDIR + "/environments"))
    updated_envs = set(get_names_of_declared_environments(settings))

    delta['new'] = updated_envs.difference(current_envs)
    delta['deleted'] = current_envs.difference(updated_envs)

    existing = updated_envs.intersection(current_envs)

    for environment in existing:
        # TODO: Cache file should always be there, but check just in case
        # and count it as changed if missing so the cache is generated again.
        hash_cache_file = open(settings.CACHEDIR + "/environments/%s" %
            environment)
        old_hash = hash_cache_file.read()
        hash_cache_file.close()
        new_hash = hash_object(settings.ENV_METADATADIR + "/%s.yaml" %
            environment)
        if old_hash == new_hash:
            delta['notchanged'].append(environment)
        else:
            delta['changed'].append(environment)

    return delta

def _resolve_branch(settings, partition, element, definition):
    overridden = False
    branch = 'master'
    if 'overrides' in definition:
        if partition in definition['overrides']:
            if element in definition['overrides'][partition].keys():
                branch = definition['overrides'][partition][element]
                logging.info("%s '%s' overridden to use treeish '%s'" %
                    (partition, element, branch))
                overridden = True
    if not overridden and 'default' in definition:
        branch = definition['default']
    return (refname_to_dirname(settings, branch), overridden)

def _link_site(settings, environment, definition):
    # LINK_NAME: $environment/site
    # TARGET: $clonedir/common/site/$branch/code
    branch, overridden = _resolve_branch(settings, 'common', 'site', definition)
    target = settings.CLONEDIR + "/common/site/%s/code" % branch
    link_name = settings.ENVIRONMENTSDIR + "/%s/site" % environment
    target = os.path.relpath(target, \
        os.path.abspath(os.path.join(link_name, os.pardir)))
    logging.debug("Linking %s to %s" % (link_name, target))
    try:
        os.symlink(target, link_name)
    except OSError, error:
        raise JensEnvironmentsError(error)

def _link_common_hieradata(settings, environment, definition):
    # Global scoped (aka, 'common') Hiera data
    # LINK_NAME: $environment/hieradata/
    # {environments, hardware, operatingsystems, common.yaml}
    # TARGET: $clonedir/common/hieradata/$branch/code/{ditto}
    branch, overridden = _resolve_branch(settings, 'common', 'hieradata', definition)
    base_target = settings.CLONEDIR + "/common/hieradata/%s/data" % branch
    base_link_name = settings.ENVIRONMENTSDIR + "/%s/hieradata" % environment

    for element in ("environments", "hardware",
            "operatingsystems", "common.yaml"):
        target = base_target + "/%s" % element
        link_name = base_link_name + "/%s" % element
        target = os.path.relpath(target, \
            os.path.abspath(os.path.join(link_name, os.pardir)))
        logging.debug("Linking %s to %s" % (link_name, target))
        try:
            os.symlink(target, link_name)
        except OSError, error:
            raise JensEnvironmentsError(error)

def _add_configuration_file(settings, environment):
    conf_file_path = "%s/%s/%s" % \
        (settings.ENVIRONMENTSDIR, environment,
        DIRECTORY_ENVIRONMENTS_CONF_FILENAME)
    conf = """modulepath = modules:hostgroups
manifest = site/site.pp
"""
    try:
        with open(conf_file_path, 'w') as conf_file:
            conf_file.write(conf)
    except IOError:
        raise JensEnvironmentsError("Unable to open %s for writing" % \
            conf_file_path)
